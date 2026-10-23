#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""知识卡（设计 16.2/16.3 的最小实现）。

知识 = 有出处、有适用范围、可被更正的主张。
不把"整页 URL"当成所有总结的支持；转载同源只算一个来源家族。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .contracts import Memory, Scope, new_id, now_ts
from .memory import MemoryService
from .repo import Repository
from .text import one_line
from .text import stable_id

SUPPORT_STATES = {
    "source_asserted", "corroborated", "tested_in_scope", "disputed", "retracted",
}
ACTIVE_STATES = {"corroborated", "tested_in_scope"}


def memory_status(support_state: str) -> str:
    if support_state in ACTIVE_STATES:
        return "active"
    return {"retracted": "archived", "disputed": "disputed"}.get(support_state, "candidate")


@dataclass
class KnowledgeCard:
    id: str
    scope: Scope
    subject: str
    memory_id: str = ""
    question: str = ""
    claims: list[dict[str, Any]] = field(default_factory=list)
    explanation: str = ""
    applicability: str = ""
    counterexamples: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    support_state: str = "source_asserted"
    source_event_ids: list[str] = field(default_factory=list)
    source_metadata: list[dict[str, Any]] = field(default_factory=list)
    acquired_at: float = field(default_factory=now_ts)
    valid_as_of: float | None = None
    review_after: float | None = None
    understanding_check_ids: list[str] = field(default_factory=list)
    application_evidence_ids: list[str] = field(default_factory=list)
    supersedes_id: str = ""
    version: int = 1


class KnowledgeService:
    def __init__(self, repo: Repository, memory: MemoryService):
        self.repo = repo
        self.memory = memory

    def add_card(self, scope: Scope, subject: str, claims: list[dict[str, Any]],
                 source_event_ids: list[str], *, expected_epoch: int | None = None,
                 **kwargs: Any) -> KnowledgeCard:
        with self.repo.tx():
            epoch = self.repo.forget_epoch(scope.scope_id)
            if expected_epoch is not None and epoch != expected_epoch:
                raise ValueError("knowledge_epoch_changed")
            for event_id in source_event_ids:
                event = self.repo.get_event(event_id)
                if (event is None or event.redacted_at is not None
                        or event.scope.private_key != scope.private_key):
                    raise ValueError("knowledge_source_removed_or_out_of_scope")
            key = kwargs.pop("idempotency_key", "")
            if key:
                identity = stable_id("know", scope.scope_id, key)
                existing = self.get_card(identity)
                if existing is not None:
                    return existing
                kwargs["card_id"] = identity
            card = self._add_card(scope, subject, claims, source_event_ids, **kwargs)
            memory = self.repo.get_memory(card.memory_id)
            if (memory is None or memory.status == "redacted"
                    or self.repo.forget_epoch(scope.scope_id) != epoch):
                raise ValueError("knowledge_commit_invalidated")
            return card

    def _add_card(self, scope: Scope, subject: str, claims: list[dict[str, Any]],
                 source_event_ids: list[str], *, question: str = "",
                 explanation: str = "", applicability: str = "",
                 limitations: list[str] | None = None,
                 support_state: str = "source_asserted",
                 source_metadata: list[dict[str, Any]] | None = None,
                 review_after: float | None = None,
                 supersedes_id: str = "", card_id: str = "") -> KnowledgeCard:
        if support_state not in SUPPORT_STATES:
            raise ValueError(f"未知支持状态：{support_state}")
        known = {e.id for e in self.repo.events_by_ids(source_event_ids)}
        cleaned = []
        for claim in claims or []:
            text = one_line(str(claim.get("text") or ""), 300)
            if not text:
                continue
            ids = [str(x) for x in (claim.get("source_event_ids") or []) if str(x) in known]
            if not ids:
                # 没有逐主张出处的主张不能进知识库
                continue
            quote = str(claim.get("quote") or "")
            if quote and not any(quote in event.text for event in self.repo.events_by_ids(ids)):
                raise ValueError("knowledge_quote_not_in_source")
            cleaned.append({"text": text, "source_event_ids": ids,
                            "quote": quote,
                            "scope_note": str(claim.get("scope_note") or "")[:200]})
        if not cleaned:
            raise ValueError("每条主张都必须绑定至少一个有效来源")
        card = KnowledgeCard(
            id=card_id or new_id("know"), scope=scope, subject=one_line(subject, 120),
            question=one_line(question, 200), claims=cleaned,
            explanation=one_line(explanation, 1200),
            applicability=one_line(applicability, 400),
            counterexamples=list(limitations or []),
            limitations=list(limitations or []),
            support_state=support_state,
            source_event_ids=list(dict.fromkeys(source_event_ids)),
            source_metadata=list(source_metadata or []),
            review_after=review_after, supersedes_id=supersedes_id,
            valid_as_of=now_ts(),
        )
        memory = self.memory.remember(
            scope, "knowledge", f"{card.subject}：{cleaned[0]['text']}",
            card.source_event_ids, source_kind="tool_observation",
            status=memory_status(support_state),
            topic_tags=[card.subject],
            source_quality=0.8 if support_state in ACTIVE_STATES else 0.5,
            uncertainty_reason=f"support_state={support_state}",
            supersedes_id=supersedes_id,
            valid_until=review_after,
        )
        card.memory_id = memory.id
        with self.repo.tx():
            self.repo._exec(
                "INSERT INTO knowledge_cards(id,memory_id,agent_id,conversation_id,subject,question,"
                "claims,explanation,applicability,counterexamples,limitations,support_state,"
                "source_event_ids,source_metadata,acquired_at,valid_as_of,review_after,"
                "understanding_check_ids,application_evidence_ids,supersedes_id,version) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (card.id, card.memory_id, scope.agent_id, scope.conversation_id, card.subject,
                 card.question, self._json(card.claims), card.explanation,
                 card.applicability, self._json(card.counterexamples),
                 self._json(card.limitations), card.support_state,
                 self._json(card.source_event_ids), self._json(card.source_metadata),
                 card.acquired_at, card.valid_as_of, card.review_after,
                 self._json(card.understanding_check_ids),
                 self._json(card.application_evidence_ids), card.supersedes_id, 1),
            )
        return card

    def confirm_card(self, card_id: str, support_state: str = "tested_in_scope",
                     *, expected_epoch: int | None = None) -> bool:
        if support_state not in SUPPORT_STATES:
            return False
        with self.repo.tx():
            card = self.get_card(card_id)
            if card is None:
                return False
            if (expected_epoch is not None
                    and self.repo.forget_epoch(card.scope.scope_id) != expected_epoch):
                return False
            memory = self.repo.get_memory(card.memory_id)
            if memory is None or memory.status == "redacted":
                return False
            for event_id in card.source_event_ids:
                event = self.repo.get_event(event_id)
                if event is None or event.redacted_at is not None:
                    return False
            self.repo._exec(
                "UPDATE knowledge_cards SET support_state=?, version=version+1 WHERE id=?",
                (support_state, card_id))
            self.repo.update_memory(
                card.memory_id, status=memory_status(support_state), updated_at=now_ts(),
                uncertainty_reason=f"support_state={support_state}")
        card.support_state = support_state
        return True

    def get_card(self, card_id: str) -> KnowledgeCard | None:
        row = self.repo._one("SELECT * FROM knowledge_cards WHERE id=?", (card_id,))
        if not row:
            return None
        return KnowledgeCard(
            id=row["id"],
            scope=Scope(row["agent_id"], row["conversation_id"]),
            memory_id=row["memory_id"] if "memory_id" in row.keys() else "",
            subject=row["subject"], question=row["question"],
            claims=self._loads(row["claims"], []), explanation=row["explanation"],
            applicability=row["applicability"],
            counterexamples=self._loads(row["counterexamples"], []),
            limitations=self._loads(row["limitations"], []),
            support_state=row["support_state"],
            source_event_ids=self._loads(row["source_event_ids"], []),
            source_metadata=self._loads(row["source_metadata"], []),
            acquired_at=float(row["acquired_at"]), valid_as_of=row["valid_as_of"],
            review_after=row["review_after"],
            understanding_check_ids=self._loads(row["understanding_check_ids"], []),
            application_evidence_ids=self._loads(row["application_evidence_ids"], []),
            supersedes_id=row["supersedes_id"], version=int(row["version"]),
        )

    def record_application(self, card_id, evidence_ids, *, expected_epoch=None):
        """Only observed, verified application can promote a learning card.

        Model self-tests may be saved as understanding checks, but cannot
        replace external conflicts or prove that a real task was completed.
        """
        with self.repo.tx():
            card = self.get_card(card_id)
            if card is None or card.support_state in {"disputed", "retracted"}:
                return False
            if expected_epoch is not None and self.repo.forget_epoch(card.scope.scope_id) != expected_epoch:
                return False
            events = self.repo.events_by_ids(evidence_ids)
            if not events or len(events) != len(set(evidence_ids)):
                return False
            for event in events:
                if (event.scope.private_key != card.scope.private_key
                        or event.source_kind != "tool_observation"
                        or event.kind != "knowledge_application"
                        or event.payload.get("card_id") != card_id
                        or event.payload.get("verified") is not True):
                    return False
            for event in events:
                self.repo._exec("INSERT OR IGNORE INTO knowledge_evidence VALUES(?,?,?,?)",
                                (card_id, "application", event.id, self._json({"at": event.occurred_at})))
            ids = list(dict.fromkeys(card.application_evidence_ids + list(evidence_ids)))
            self.repo._exec("UPDATE knowledge_cards SET application_evidence_ids=? WHERE id=?", (self._json(ids), card_id))
            return self.confirm_card(card_id, "tested_in_scope", expected_epoch=expected_epoch)

    @staticmethod
    def _json(value: Any) -> str:
        import json
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _loads(value: str, default: Any) -> Any:
        import json
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return default
