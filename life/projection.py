#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把原始事件投影成经历/事实/未完成事项。

首版不调用模型：用确定性规则做最小投影，保证离线可测、可重放。
以后换成 LLM 提取时，接口不变，但要继续满足：
- 只引用本批次真实存在的事件 ID；
- 同一批次只提交一次；
- 事实修订走 MemoryService.revise_fact，不整批覆盖。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .contracts import Event, Memory, OpenLoop, Scope, new_id, now_ts
from .memory import MemoryService
from .repo import Repository
from .text import one_line, stable_id


@dataclass
class ProjectionConfig:
    max_events_per_batch: int = 40
    episode_min_chars: int = 6


DEFAULT_PROJECTION = ProjectionConfig()

_FACT_PATTERNS = (
    ("name", re.compile(r"(?:我叫|我的名字是|叫我)([^\s，。！？,!.?]{1,20})")),
    ("likes", re.compile(r"我(?:很)?喜欢([^\s，。！？,!.?]{1,30})")),
    ("dislikes", re.compile(r"我(?:很)?(?:不喜欢|讨厌)([^\s，。！？,!.?]{1,30})")),
    ("remember", re.compile(r"(?:记住|记一下|别忘了)[：:，,\s]*([^。！？\n]{2,60})")),
    ("preference", re.compile(r"(?:以后|下次)([^。！？\n]{2,50})")),
)
_PRED_LABEL = {
    "name": "叫",
    "likes": "喜欢",
    "dislikes": "不喜欢",
    "remember": "让他记住",
    "preference": "说过以后",
}
_LOOP_PATTERNS = (
    ("request", re.compile(r"(?:帮我|替我|给我)([^。！？\n]{2,60})")),
    ("commitment", re.compile(r"(?:提醒我|记得|答应你|答应我)([^。！？\n]{2,60})")),
)


class Projector:
    def __init__(self, repo: Repository, memory: MemoryService,
                 config: ProjectionConfig | None = None):
        self.repo = repo
        self.memory = memory
        self.cfg = config or DEFAULT_PROJECTION

    def cursor(self, scope: Scope) -> int:
        key = f"projection_cursor:{scope.scope_id}"
        row = self.repo._one("SELECT v FROM meta WHERE k=?", (key,))
        return int(row["v"]) if row else 0

    def _set_cursor(self, scope: Scope, seq: int) -> None:
        key = f"projection_cursor:{scope.scope_id}"
        with self.repo.tx():
            self.repo._exec("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (key, str(int(seq))))

    def project_pending(self, scope: Scope) -> dict[str, int]:
        with self.repo.tx():
            return self._project_pending(scope)

    def _project_pending(self, scope: Scope) -> dict[str, int]:
        """把一个作用域里尚未投影的事件按批处理。单 worker 串行。"""
        after = self.cursor(scope)
        events = self.repo.events_after(scope, after, limit=self.cfg.max_events_per_batch)
        if not events:
            return {"batches": 0, "episodes": 0, "facts": 0, "loops": 0}
        result = self.project_batch(scope, events)
        if not result.get("aborted"):
            self._set_cursor(scope, max(e.ingest_seq for e in events))
        return result

    def project_batch(self, scope: Scope, events: list[Event]) -> dict[str, int]:
        with self.repo.tx():
            return self._project_batch(scope, events)

    def _project_batch(self, scope: Scope, events: list[Event]) -> dict[str, int]:
        if not events:
            return {"batches": 0, "episodes": 0, "facts": 0, "loops": 0}
        batch_id = stable_id("batch", *[e.id for e in events])
        if self.repo.batch_committed(scope.scope_id, "deterministic", batch_id):
            return {"batches": 0, "episodes": 0, "facts": 0, "loops": 0}
        epoch = self.repo.forget_epoch(scope.scope_id)
        # 只用当前仍有效的事件；删除后缓存的旧事件不能再投影
        events = self.repo.events_by_ids([e.id for e in events])
        if not events:
            return {"batches": 0, "episodes": 0, "facts": 0, "loops": 0, "aborted": 1}

        episodes = facts = loops = 0
        for group in self._group_turns(events):
            summary = self._episode_summary(group)
            if summary and len(summary) >= self.cfg.episode_min_chars:
                # 同一事件组只投影一次：即使上次在写批次标记前崩溃，重试也不重复
                if self.repo.find_episode_by_group(scope, group[0].id) is None:
                    episode = self.memory.remember_episode(
                        scope, summary, [e.id for e in group],
                        salience=self._salience(group), event_group_id=group[0].id,
                        source_kind="asserted",
                    )
                    if episode.status != "redacted":
                        episodes += 1
            for event in group:
                if event.source_kind != "user_report":
                    continue
                facts += self._extract_facts(scope, event)
                loops += self._extract_loops(scope, event)

        if self.repo.forget_epoch(scope.scope_id) != epoch:
            return {"batches": 0, "episodes": episodes, "facts": facts,
                    "loops": loops, "aborted": 1}
        self.repo.mark_batch_committed(scope.scope_id, "deterministic", batch_id)
        return {"batches": 1, "episodes": episodes, "facts": facts, "loops": loops}

    @staticmethod
    def _group_turns(events: list[Event]) -> list[list[Event]]:
        groups: list[list[Event]] = []
        current: list[Event] = []
        for event in events:
            if event.kind != "message":
                if current:
                    groups.append(current)
                    current = []
                continue
            if event.source_kind == "user_report" and current:
                groups.append(current)
                current = [event]
            else:
                current.append(event)
        if current:
            groups.append(current)
        return groups

    @staticmethod
    def _salience(group: list[Event]) -> float:
        text = " ".join(e.text for e in group)
        score = 0.35
        if any(w in text for w in ("第一次", "记住", "答应", "对不起", "谢谢")):
            score += 0.25
        if len(text) > 120:
            score += 0.1
        return min(1.0, score)

    @staticmethod
    def _episode_summary(group: list[Event]) -> str:
        parts = []
        for event in group:
            who = "他" if event.source_kind == "user_report" else "我"
            text = one_line(event.text, 180)
            if text:
                parts.append(f"{who}：{text}")
        return one_line(" / ".join(parts), 480)

    def _extract_facts(self, scope: Scope, event: Event) -> int:
        text = event.text
        if not text:
            return 0
        made = 0
        for predicate, pattern in _FACT_PATTERNS:
            m = pattern.search(text)
            if not m:
                continue
            value = m.group(1).strip(" ：:，,")
            if len(value) < 2:
                continue
            subject = event.scope.participant_id or event.actor_id or "user"
            self.memory.revise_fact(
                scope, subject, predicate, value, [event.id],
                summary=f"他{_PRED_LABEL.get(predicate, predicate)}：{value}",
                source_kind="user_report",
                source_quality=0.7,
                explicit_correction=(predicate == "remember"),
            )
            made += 1
        return made

    def _extract_loops(self, scope: Scope, event: Event) -> int:
        from .control import control_kind
        if control_kind(event.text) or self.repo._one("SELECT 1 FROM meta WHERE k=?", ("stopped_root:" + scope.scope_id + ":" + event.id,)):
            return 0
        text = event.text
        if not text:
            return 0
        made = 0
        for kind, pattern in _LOOP_PATTERNS:
            m = pattern.search(text)
            if not m:
                continue
            if self.repo.open_loop_exists_for_event(scope, event.id):
                continue
            summary = one_line(f"他让我：{m.group(1)}", 120)
            loop = OpenLoop(
                id=new_id("loop"), scope=scope, kind=kind, summary=summary,
                source_event_ids=[event.id], status="proposed",
                next_review_at=now_ts() + 3600,
            )
            self.repo.insert_open_loop(loop)
            made += 1
        return made
