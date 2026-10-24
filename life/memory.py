#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""记忆服务：来源、作用域、版本、检索预算、遗忘。

首版是词法检索（设计 6.4 的基线），但接口按以后可替换成向量/混合检索来定。
关键约束：
- 读取不产生副作用（T14/T25）；
- 检索先在候选阶段按作用域过滤，不先拿私密内容再让模型自觉避开；
- 事实冲突不整批覆盖，按 revise_fact 的分支处理；
- 删除用 forget_epoch 做在途屏障。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .contracts import (
    Memory, MemorySource, OpenLoop, RetrievalPacket, Scope, clamp, new_id,
    now_ts,
)
from .repo import Repository
from .text import estimate_tokens, normalize_value, one_line, terms as text_terms


@dataclass
class MemoryConfig:
    token_budget: int = 1200
    history_budget: int = 2400
    candidate_limit: int = 80
    max_items: int = 24
    max_per_event_group: int = 2
    # 设计 6.4 的初始权重
    w_relevance: float = 0.40
    w_continuity: float = 0.20
    w_quality: float = 0.15
    w_recency: float = 0.15
    w_salience: float = 0.10
    mention_penalty: float = 0.02
    conflict_penalty: float = 0.20


DEFAULT_MEMORY = MemoryConfig()


class MemoryService:
    def __init__(self, repo: Repository, config: MemoryConfig | None = None):
        self.repo = repo
        self.cfg = config or DEFAULT_MEMORY

    # ---------------------------------------------------------------- 写入

    def remember(self, scope: Scope, kind: str, summary: str,
                 source_event_ids: list[str], *,
                 source_kind: str = "inferred",
                 status: str = "active",
                 subject_id: str = "", predicate: str = "",
                 normalized_value: str = "",
                 event_group_id: str = "",
                 topic_tags: list[str] | None = None,
                 valid_from: float | None = None,
                 valid_until: float | None = None,
                 salience: float = 0.5,
                 source_quality: float = 0.5,
                 uncertainty_reason: str = "",
                 supersedes_id: str = "",
                 derived_from: list[str] | None = None,
                 extra_terms: list[str] | None = None) -> Memory:
        ts = now_ts()
        expected_epoch = self.repo.forget_epoch(scope.scope_id)
        valid_sources: list[str] = []
        for eid in dict.fromkeys(source_event_ids):
            event = self.repo.get_event(str(eid))
            if event is None or event.redacted_at is not None:
                # A summary of several sources cannot keep its text after silently
                # discarding one of those sources. It must be derived again.
                return Memory(id=new_id("mem"), scope=scope, kind=kind, summary="",
                              source_kind=source_kind, status="redacted",
                              uncertainty_reason="source_missing_or_redacted")
            if event.scope.agent_id != scope.agent_id or                     event.scope.conversation_id != scope.conversation_id:
                # 跨作用域引用直接拒绝，不能把 A 的私聊来源挂到 B 的记忆上
                return Memory(id=new_id("mem"), scope=scope, kind=kind, summary="",
                              source_kind=source_kind, status="redacted",
                              uncertainty_reason="source_scope_mismatch")
            valid_sources.append(str(eid))
        if source_event_ids and not valid_sources:
            return Memory(id=new_id("mem"), scope=scope, kind=kind, summary="",
                          source_kind=source_kind, status="redacted",
                          uncertainty_reason="source_missing_or_redacted")
        memory = Memory(
            id=new_id("mem"), scope=scope, kind=kind, summary=one_line(summary, 600),
            source_kind=source_kind, status=status, subject_id=subject_id,
            predicate=predicate, normalized_value=normalized_value,
            event_group_id=event_group_id, topic_tags=list(topic_tags or []),
            valid_from=valid_from, valid_until=valid_until,
            recorded_at=ts, updated_at=ts, salience=clamp(salience, 0, 1),
            source_quality=clamp(source_quality, 0, 1),
            uncertainty_reason=uncertainty_reason, supersedes_id=supersedes_id,
            derived_from=list(derived_from or []),
        )
        memory.sources = [MemorySource(memory.id, eid) for eid in valid_sources]
        word_list = text_terms(" ".join([summary, subject_id, predicate,
                                         " ".join(topic_tags or []), *(extra_terms or [])]))
        if not self.repo.insert_memory_if_current(memory, word_list, expected_epoch):
            memory.status = "redacted"
            memory.summary = ""
            memory.uncertainty_reason = "stale_epoch_or_source_removed"
        return memory

    def remember_episode(self, scope: Scope, summary: str, event_ids: list[str],
                         *, salience: float = 0.6, event_group_id: str = "",
                         source_kind: str = "asserted",
                         tags: list[str] | None = None) -> Memory:
        return self.remember(scope, "episode", summary, event_ids,
                             source_kind=source_kind, status="active",
                             event_group_id=event_group_id or (event_ids[0] if event_ids else ""),
                             salience=salience, source_quality=0.8, topic_tags=tags)

    def revise_fact(self, scope: Scope, subject_id: str, predicate: str, value: str,
                    source_event_ids: list[str], *, summary: str = "",
                    source_kind: str = "user_report",
                    source_quality: float = 0.8,
                    explicit_correction: bool = False,
                    explicit_change: bool = False,
                    valid_from: float | None = None) -> Memory:
        """按设计 6.3：合并 / 纠正 / 变化 / 冲突 / 新增 分开处理。"""
        norm = normalize_value(value)
        current = self.repo.find_facts(scope, subject_id, predicate)
        sources = list(dict.fromkeys(source_event_ids))
        valid_sources = []
        for eid in sources:
            event = self.repo.get_event(str(eid))
            if (event is None or event.redacted_at is not None
                    or event.scope.agent_id != scope.agent_id
                    or event.scope.conversation_id != scope.conversation_id):
                return Memory(id=new_id("mem"), scope=scope, kind="fact", summary="",
                              source_kind=source_kind, status="redacted",
                              uncertainty_reason="source_scope_mismatch")
            valid_sources.append(str(eid))
        sources = valid_sources

        for old in current:
            old_sources = {s.event_id for s in old.sources}
            if old_sources & set(sources):
                return old  # 同一根事件已经处理过

        same = [m for m in current if m.normalized_value == norm]
        if same:
            target = same[0]
            for eid in sources:
                self.repo.add_memory_source(target.id, MemorySource(target.id, eid))
            self.repo.update_memory(target.id, updated_at=now_ts(),
                                    source_quality=max(target.source_quality, source_quality))
            return target

        if explicit_correction and current:
            for old in current:
                self.repo.update_memory(old.id, status="superseded", valid_until=now_ts(),
                                        updated_at=now_ts())
            return self._insert_fact(scope, subject_id, predicate, norm, summary or value,
                                     sources, source_kind, "active", source_quality,
                                     valid_from, supersedes=current[0].id)

        if explicit_change and current:
            for old in current:
                self.repo.update_memory(old.id, status="superseded",
                                        valid_until=valid_from or now_ts(),
                                        updated_at=now_ts())
            return self._insert_fact(scope, subject_id, predicate, norm, summary or value,
                                     sources, source_kind, "active", source_quality,
                                     valid_from, supersedes=current[0].id)

        if current:
            # 有依据但和现有值不一致：两边都留着，标 disputed，不进确定事实区
            return self._insert_fact(scope, subject_id, predicate, norm, summary or value,
                                     sources, source_kind, "disputed", source_quality,
                                     valid_from, supersedes="")

        status = "active" if source_quality >= 0.5 else "candidate"
        return self._insert_fact(scope, subject_id, predicate, norm, summary or value,
                                 sources, source_kind, status, source_quality,
                                 valid_from, supersedes="")

    def _insert_fact(self, scope: Scope, subject: str, predicate: str, norm: str,
                     summary: str, sources: list[str], source_kind: str, status: str,
                     quality: float, valid_from: float | None, supersedes: str) -> Memory:
        return self.remember(
            scope, "fact", summary, sources,
            source_kind=source_kind, status=status, subject_id=subject,
            predicate=predicate, normalized_value=norm,
            valid_from=valid_from, salience=0.65, source_quality=quality,
            supersedes_id=supersedes,
            extra_terms=[subject, predicate, norm],
        )

    # ---------------------------------------------------------------- 检索

    def retrieve(self, scope: Scope, query: str, now: float | None = None,
                 budget: int | None = None, mode: str = "current", *, _queries=None, _exclude=()) -> RetrievalPacket:
        now = now_ts() if now is None else now
        budget = max(0, int(self.cfg.token_budget if budget is None else budget))
        queries = [query or ''] if _queries is None else list(_queries)[:3]
        qterms = list(dict.fromkeys(t for q in queries for t in text_terms(q)[:96]))
        statuses = ["active", "disputed"] if mode == "current" else [
            "active", "disputed", "superseded", "archived"]

        candidates = []
        for cue in queries:
            candidates += self.repo.search_memories(scope,text_terms(cue)[:96],statuses,
                limit=20 if _queries is not None else self.cfg.candidate_limit)
        candidates += self.repo.recent_memories(scope, statuses, limit=30)
        if getattr(self, 'recollection', None):
            candidates += self.recollection.expansion(scope, query)
        # Unverified knowledge can be cited as a source assertion, never as a
        # verified fact. Other candidate memories keep their existing policy.
        candidates += [memory for memory in self.repo.recent_memories(scope, ["candidate"], limit=30)
                       if memory.kind == "knowledge" and memory.source_kind == "tool_observation"]
        by_id: dict[str, Memory] = {m.id: m for m in candidates if m.id not in _exclude}
        loops = self.repo.active_open_loops(scope, limit=8)
        loop_terms = {l.id: set(text_terms(l.summary + " " + l.next_step)) for l in loops}

        scored: list[tuple[float, Memory]] = []
        boosts = self.recollection.boosts(scope, query) if hasattr(self, 'recollection') else {}
        for m in by_id.values():
            sources = self.repo.events_by_ids(s.event_id for s in m.sources)
            if (len(sources) != len({s.event_id for s in m.sources})
                    or m.scope.audience_policy_id != scope.audience_policy_id
                    or any(source.scope.private_key != scope.private_key or
                           source.scope.audience_policy_id != scope.audience_policy_id for source in sources)):
                continue
            mterms = set(self.repo.memory_terms(m.id))
            relevance = (len(set(qterms) & mterms) / max(1, len(set(qterms)))) if qterms else 0.0
            continuity = 1.0 if any(mterms & lt for lt in loop_terms.values()) else 0.0
            age_days = max(0.0, (now - float(m.updated_at)) / 86400.0)
            recency = 0.5 ** (age_days / 30.0)
            score = (self.cfg.w_relevance * relevance
                     + self.cfg.w_continuity * continuity
                     + self.cfg.w_quality * float(m.source_quality)
                     + self.cfg.w_recency * recency
                     + self.cfg.w_salience * float(m.salience)
                     - self.cfg.mention_penalty * int(m.mention_count_window)
                     - (self.cfg.conflict_penalty if m.status == "disputed" else 0.0))
            scored.append((score + boosts.get(m.id, 0), m))

        scored.sort(key=lambda x: (-x[0], -x[1].updated_at))
        chosen: list[Memory] = []
        per_group: dict[str, int] = {}
        seen_evidence = set()
        if _queries is not None:
            for identity in _exclude:
                item = self.repo.get_memory(identity)
                if item and item.scope.private_key==scope.private_key:
                    seen_evidence.update(s.event_id for s in item.sources)
        for _score, m in scored:
            evidence = {s.event_id for s in m.sources}
            if _queries is not None and evidence & seen_evidence:
                continue
            group = m.event_group_id or m.id
            if per_group.get(group, 0) >= self.cfg.max_per_event_group:
                continue
            per_group[group] = per_group.get(group, 0) + 1
            chosen.append(m)
            seen_evidence.update(evidence)
            if len(chosen) >= self.cfg.max_items:
                break

        packet = RetrievalPacket(scope=scope, open_loops=loops)
        for m in chosen:
            if m.kind == "fact":
                (packet.uncertain if m.status == "disputed" else packet.facts).append(m)
            elif m.kind == "episode":
                packet.episodes.append(m)
            elif m.kind in ("knowledge", "lesson"):
                (packet.uncertain if m.status in {"disputed", "candidate"}
                 or (m.valid_until is not None and m.valid_until <= now) else packet.knowledge).append(m)
            elif m.kind in ("diary", "interpretation"):
                packet.interpretations.append(m)
            else:
                packet.uncertain.append(m)

        packet.text, packet.token_estimate, packet.missing = self._render(packet, budget, now)
        return packet

    def retrieve_multi(self, scope, cues, *, budget, exclude=()):
        """At most three lexical routes, merged before a single budget is rendered."""
        return self.retrieve(scope,' '.join(cues),budget=budget,_queries=cues,_exclude=exclude)

    def _render(self, packet: RetrievalPacket, budget: int, now: float | None = None) -> tuple[str, int, list[str]]:
        now = now_ts() if now is None else now
        lines: list[str] = []
        missing: list[str] = []

        def add(title: str, items: list[str]) -> None:
            if not items:
                return
            accepted = []
            for item in items:
                trial = f"\n## {title}\n" + '\n'.join('- ' + x for x in accepted + [item]) + '\n'
                if estimate_tokens(''.join(lines) + trial) <= budget:
                    accepted.append(item)
                elif title not in missing:
                    missing.append(title)
            if accepted:
                lines.append(f"\n## {title}\n" + '\n'.join('- ' + x for x in accepted) + '\n')

        def cited(memory):
            sources = ",".join(s.event_id for s in memory.sources[:3])
            expiry = "；已到复核时间，未核对当前有效性" if memory.valid_until is not None and memory.valid_until <= now else ""
            return memory.summary + (f" [来源:{sources}{expiry}]" if sources else " [无原文来源]")
        add("关于他的确定事实", [cited(m) for m in packet.facts])
        add("还没做完的事", [f"{l.summary}（{l.status}）" for l in packet.open_loops])
        add("相关的共同经历", [cited(m) for m in packet.episodes])
        add("学到的东西", [cited(m) for m in packet.knowledge])
        add("我当时的理解（不是外部事实）", [cited(m) for m in packet.interpretations])
        add("不确定、需要核对", [cited(m) for m in packet.uncertain])
        text = "".join(lines).strip()
        return text, estimate_tokens(text), missing

    # ---------------------------------------------------------------- 遗忘

    def forget(self, scope: Scope, memory_ids: list[str] | None = None,
               kinds: list[str] | None = None) -> dict[str, Any]:
        with self.repo.tx():
            return self._forget(scope, memory_ids, kinds)

    def _forget(self, scope: Scope, memory_ids: list[str] | None,
                kinds: list[str] | None) -> dict[str, Any]:
        """局部遗忘：摘要/索引/待办一起失效，并推进 forget_epoch。"""
        targets: list[Memory] = []
        if memory_ids:
            targets = self.repo.memories_by_ids(memory_ids)
        elif kinds:
            pool = self.repo.recent_memories(
                scope, ["active", "disputed", "candidate"], limit=1000)
            targets = [m for m in pool if m.kind in kinds]
        allowed = [m for m in targets
                   if m.scope.agent_id == scope.agent_id
                   and m.scope.conversation_id == scope.conversation_id]
        denied = len(targets) - len(allowed)
        # Fence old writers before expanding lineage, under the same write lock.
        epoch = self.repo.bump_forget_epoch(scope.scope_id)
        event_ids: list[str] = []
        for memory in allowed:
            event_ids += [s.event_id for s in memory.sources]
        event_ids = list(dict.fromkeys(event_ids))
        # Linked reminders are invalidated by their sources; no forgotten text
        # remains available through an annotation or salience mark.
        for row in self.repo._all("SELECT id,sources,memory_id FROM memory_annotations WHERE scope_id=? AND status='active'", (scope.scope_id,)):
            import json
            if set(json.loads(row['sources'])) & set(event_ids) or row['memory_id'] in {m.id for m in allowed}:
                self.repo._exec("UPDATE memory_annotations SET text='',cues='[]',sources='[]',status='redacted' WHERE id=?", (row['id'],))
        # 删除闭包：所有引用同一批根事件的摘要/日记/知识/派生记忆一起失效
        closure = set(self.repo.memory_ids_sharing_events(event_ids))
        ids = self.repo.memory_ids_derived_from(
            list(set(m.id for m in allowed) | closure))
        for identity in ids:
            for annotation in self.repo._all('SELECT id FROM memory_annotations WHERE scope_id=? AND memory_id=?', (scope.scope_id, identity)):
                self.repo._exec("UPDATE memory_annotations SET text='',cues='[]',sources='[]',skill_refs='[]',status='redacted' WHERE id=?", (annotation['id'],))
                self.repo._exec('DELETE FROM annotation_terms WHERE annotation_id=?', (annotation['id'],))
        self.repo._exec("DELETE FROM annotation_terms WHERE annotation_id IN (SELECT id FROM memory_annotations WHERE scope_id=? AND status='redacted')", (scope.scope_id,))
        ledger = getattr(self, "tombstones", None)
        if ledger is not None:
            identity = ledger.append(scope, ids, event_ids)
        redacted = self.repo.redact_memories(ids, epoch)
        events = self.repo.redact_events(event_ids)
        loops = self.repo.cancel_open_loops_for_events(event_ids)
        cards = self.repo.redact_knowledge_cards_for_events(event_ids)
        if ledger is not None:
            ledger.apply(identity, scope, ids, event_ids, epoch=epoch)
        return {"epoch": epoch, "redacted": redacted, "events": events,
                "loops_cancelled": loops, "knowledge_cards": cards,
                "denied": denied, "memory_ids": ids[:50]}

    def explain(self, memory_id: str, scope: Scope | None = None) -> dict[str, Any] | None:
        m = self.repo.get_memory(memory_id)
        if not m:
            return None
        if (scope is None or m.scope.agent_id != scope.agent_id
                or m.scope.conversation_id != scope.conversation_id):
            return {"id": memory_id, "error": "access_denied", "summary": ""}
        if m.status == "redacted":
            return {"id": memory_id, "error": "redacted", "summary": ""}
        return {
            "id": m.id, "kind": m.kind, "status": m.status,
            "summary": m.summary, "source_kind": m.source_kind,
            "scope": {"agent": m.scope.agent_id, "conversation": m.scope.conversation_id},
            "sources": [{"event_id": s.event_id, "relation": s.relation} for s in m.sources],
            "valid_from": m.valid_from, "valid_until": m.valid_until,
            "supersedes": m.supersedes_id, "uncertainty": m.uncertainty_reason,
        }
