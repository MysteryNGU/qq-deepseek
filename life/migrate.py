#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""旧库迁移（设计 12.1/12.2 的最小可用版）。

原则：
- 逐行映射，可重复执行；
- 旧消息没有可靠的群/私聊来源，默认放进 `legacy_unknown` 作用域，
  不猜测为可在群公开；
- 旧事实/日记没有原话来源，标 `legacy_unverified` / candidate；
- 本次不删除旧库，也不改变旧库。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .runtime import LifeRuntime


@dataclass
class MigrationReport:
    dry_run: bool = True
    messages: int = 0
    facts: int = 0
    diaries: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


def _legacy_scope(runtime: LifeRuntime, peer: int, agent_id: str):
    conv = f"qq:legacy_unknown:{peer}"
    return runtime.scope(conv, str(peer), "legacy_unknown_scope")


def migrate_brain(brain_db: str | Path, runtime: LifeRuntime, *,
                  agent_id: str | None = None, dry_run: bool = True,
                  limit: int | None = None) -> MigrationReport:
    db_path = Path(brain_db)
    report = MigrationReport(dry_run=dry_run)
    if not db_path.exists():
        report.errors.append(f"找不到旧库：{db_path}")
        return report
    agent = str(agent_id or runtime.agent_id)
    source_db = str(db_path.resolve())
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        tables = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}

        if "msg" in tables:
            rows = conn.execute(
                "SELECT id,peer,role,content,ts FROM msg ORDER BY id").fetchall()
            for row in (rows[:limit] if limit else rows):
                pk = str(row["id"])
                if runtime.repo.legacy_mapped(source_db, "msg", pk):
                    report.skipped += 1
                    continue
                if dry_run:
                    report.messages += 1
                    continue
                scope = _legacy_scope(runtime, int(row["peer"]), agent)
                event, _created = runtime.ingest(
                    scope, "message", "legacy_unverified", row["content"],
                    external_id=f"brain:msg:{pk}", role=row["role"],
                    occurred_at=float(row["ts"]),
                )
                runtime.repo.record_legacy_mapping(
                    source_db, "msg", pk, "event", event.id)
                report.messages += 1

        if "fact" in tables:
            rows = conn.execute(
                "SELECT id,peer,content,ts FROM fact ORDER BY id").fetchall()
            for row in (rows[:limit] if limit else rows):
                pk = str(row["id"])
                if runtime.repo.legacy_mapped(source_db, "fact", pk):
                    report.skipped += 1
                    continue
                if dry_run:
                    report.facts += 1
                    continue
                scope = _legacy_scope(runtime, int(row["peer"]), agent)
                content = str(row["content"])
                # 旧 fact 表混进了"经验/教训"，别把它当关于他的事实
                is_lesson = content.startswith(("我自己试出来", "记得先", "别连着"))
                mem = runtime.memory.remember(
                    scope, "lesson" if is_lesson else "fact", content, [],
                    source_kind="legacy_unverified", status="candidate",
                    subject_id="" if is_lesson else str(row["peer"]),
                    predicate="" if is_lesson else "legacy_fact",
                    uncertainty_reason="旧库导入：没有原话来源，需用户确认后才可当事实",
                    source_quality=0.35,
                )
                runtime.repo.record_legacy_mapping(
                    source_db, "fact", pk, "memory", mem.id)
                report.facts += 1

        if "diary" in tables:
            rows = conn.execute(
                "SELECT id,peer,day,content,ts FROM diary ORDER BY id").fetchall()
            for row in (rows[:limit] if limit else rows):
                pk = str(row["id"])
                if runtime.repo.legacy_mapped(source_db, "diary", pk):
                    report.skipped += 1
                    continue
                if dry_run:
                    report.diaries += 1
                    continue
                scope = _legacy_scope(runtime, int(row["peer"]), agent)
                mem = runtime.memory.remember(
                    scope, "interpretation", str(row["content"]), [],
                    source_kind="legacy_unverified", status="candidate",
                    uncertainty_reason=f"旧日记（{row['day']}），是当时的解释，不是外部事实",
                    source_quality=0.3,
                )
                runtime.repo.record_legacy_mapping(
                    source_db, "diary", pk, "memory", mem.id)
                report.diaries += 1
    finally:
        conn.close()
    return report
