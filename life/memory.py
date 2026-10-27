#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""记忆服务：来源、作用域、版本、检索预算、遗忘。

事实写入、修订和遗忘；所有召回只经 LifeRecall。
关键约束：
- 读取不产生副作用（T14/T25）；
- 检索先在候选阶段按作用域过滤，不先拿私密内容再让模型自觉避开；
- 事实冲突不整批覆盖，按 revise_fact 的分支处理；
- 删除用 forget_epoch 做在途屏障。
"""

from __future__ import annotations

from typing import Any

from .contracts import (
    Memory, MemorySource, Scope, clamp, new_id,
    now_ts,
)
from .repo import Repository
from .text import estimate_tokens, normalize_value, one_line, terms as text_terms
from .memory_policy import (topic_key as resolve_topic, inference_expiry, interpretation_current, source_deadline,
                            MAX_INTERPRETATIONS)


class MemoryService:
    def __init__(self, repo: Repository):
        self.repo = repo

    def archive_expired_interpretations(self, agent_id, *, now=None, limit=100):
        """Bounded housekeeping; preserve the notes and their original evidence."""
        ts = now_ts() if now is None else now
        with self.repo.tx():
            rows = self.repo._all("SELECT id,version FROM memories WHERE agent_id=? "
                "AND kind='interpretation' AND status IN ('active','disputed') "
                "AND COALESCE(valid_until,recorded_at+604800)<=? ORDER BY recorded_at LIMIT ?",
                (agent_id,ts,limit))
            for row in rows:
                self.repo.update_memory(row['id'],status='archived',version=row['version']+1,updated_at=ts)
        return len(rows)

    # ---------------------------------------------------------------- 写入

    def commit_organized_memory(self, internal_binding, lease, proposal, call_key):
        """Trusted multi-source entry; ordinary remember keeps its scope checks."""
        organizer = getattr(self, 'organizer', None)
        if organizer is None:
            raise ValueError('memory_organize_not_ready')
        return organizer.commit(internal_binding, lease, proposal, call_key)

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
                 extra_terms: list[str] | None = None,
                 topic_key: str = '') -> Memory:
        with self.repo.tx():
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
                if event.scope.agent_id != scope.agent_id or event.scope.conversation_id != scope.conversation_id or event.scope.audience_policy_id != scope.audience_policy_id:
                    # 跨作用域引用直接拒绝，不能把 A 的私聊来源挂到 B 的记忆上
                    return Memory(id=new_id("mem"), scope=scope, kind=kind, summary="",
                                  source_kind=source_kind, status="redacted",
                                  uncertainty_reason="source_scope_mismatch")
                valid_sources.append(str(eid))
            if source_event_ids and not valid_sources:
                return Memory(id=new_id("mem"), scope=scope, kind=kind, summary="",
                              source_kind=source_kind, status="redacted",
                              uncertainty_reason="source_missing_or_redacted")
            previous = None
            if kind == 'interpretation':
                valid_until = min(valid_until, inference_expiry(now=ts)) if valid_until is not None else inference_expiry(now=ts)
                if 'model-derived' in (topic_tags or []) and valid_sources:
                    evidence_time = max(self.repo.get_event(eid).received_at for eid in valid_sources)
                    valid_until = min(valid_until,inference_expiry(now=evidence_time))
                valid_until = source_deadline(self.repo,valid_sources,valid_until,ts,scope)
                key = resolve_topic(topic_key, summary)
                rows = self.repo._all("SELECT id FROM memories WHERE agent_id=? AND conversation_id=? AND audience_policy_id=? "
                                      "AND kind='interpretation' AND status IN ('active','disputed') ORDER BY updated_at DESC",
                                      (*scope.private_key,scope.audience_policy_id))
                current = []
                for row in rows:
                    item = self.repo.get_memory(row['id'])
                    if not interpretation_current(self.repo,item,ts):
                        self.repo.update_memory(item.id,status='archived',version=item.version+1,updated_at=ts)
                        continue
                    current.append(item)
                    if (item.predicate or resolve_topic('',item.summary)) == key:
                        if item.summary == one_line(summary,600) and item.topic_tags == list(topic_tags or []):
                            return item  # Repetition does not renew validity or raise confidence.
                        previous = item
                if previous is None and len(current) >= MAX_INTERPRETATIONS:
                    raise ValueError('interpretations_full: replace or archive an existing topic')
                predicate = key
                if previous is not None:
                    supersedes_id = previous.id
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
            with self.repo.tx():
                if not self.repo.insert_memory_if_current(memory, word_list, expected_epoch):
                    memory.status = "redacted"
                    memory.summary = ""
                    memory.uncertainty_reason = "stale_epoch_or_source_removed"
                elif previous is not None:
                    self.repo.update_memory(previous.id,status='superseded',version=previous.version+1,updated_at=ts)
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
                or m.scope.conversation_id != scope.conversation_id or m.scope.audience_policy_id!=scope.audience_policy_id):
            return {"id": memory_id, "error": "access_denied", "summary": ""}
        if m.status == "redacted":
            return {"id": memory_id, "error": "redacted", "summary": ""}
        from .life_access import RecallError
        try:
            self.access.load(self.access.bind(scope),m.id)
        except RecallError:
            return {"id":memory_id,"error":"access_denied","summary":""}
        from .learning_evidence import scoped_sources,user_statements
        try:
            quotes=user_statements(scoped_sources(self.repo,scope,[s.event_id for s in m.sources]))
        except ValueError:
            return {"id":memory_id,"error":"source_removed","summary":""}
        if m.kind=='fact' and m.source_kind=='user_report':
            m.summary='\n'.join(q['text'] for q in quotes)
        return {
            "id": m.id, "kind": m.kind, "status": m.status,
            "summary": m.summary, "source_kind": m.source_kind,
            "scope": {"agent": m.scope.agent_id, "conversation": m.scope.conversation_id},
            "sources": [{"event_id": s.event_id, "relation": s.relation} for s in m.sources],
            "valid_from": m.valid_from, "valid_until": m.valid_until,
            "supersedes": m.supersedes_id, "uncertainty": m.uncertainty_reason,
            "authority": "model_interpretation" if m.kind=='interpretation' else "sourced_summary",
            "source_quotes":quotes,
            "topic_tags": m.topic_tags,
        }
