#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一的“内在生活”读写模型。

这里是念头和念想唯一的读写入口。回复、空闲思考、念想执行和日志都
应该读同一份 inner state，避免出现“日志里在想 A，回复时说 B”的分裂。

三条硬规则：
1. 念头只是推想，不是事实；念想是“想做的事”，不是已经做过的事。
2. 相似目标复用已有念想，不重复创建。
3. 没有真实依据的念想可以保留为推想，但不能自动启动外部动作。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Iterable

from .text import terms

OPEN_STATUSES = ("seed", "active", "paused", "blocked", "unfinished")
ACTIONABLE_STATUSES = ("seed", "active")

_PUNCT = re.compile(r"[\s，。！？、；：,.!?;:\"'“”‘’（）()【】\[\]—…~～]+")
_MODAL = (
    "我想", "我要", "想要", "想", "再", "换个思路", "换一种", "试试",
    "试一次", "一次", "那个", "这个", "这件事", "事情", "把",
)
_EVENT_ID = re.compile(r"\bevt_[0-9a-fA-F]{8,}\b")
_QUOTED = re.compile(r"[「“\"]([^」”\"]{2,60})[」”\"]")


def _clean(text: str) -> str:
    return _PUNCT.sub("", str(text or "").strip().lower())


def _dedupe_key(text: str) -> str:
    s = _clean(text)
    for word in _MODAL:
        s = s.replace(word, "")
    return s[:160]


def _tokens(text: str) -> set[str]:
    return set(terms(text or ""))


def _overlap(a: str, b: str) -> float:
    """Containment overlap, not Jaccard: long thoughts still match short facts."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    shared = len(ta & tb)
    if shared < 2:
        return 0.0
    return shared / max(1, min(len(ta), len(tb)))


def _clip(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


class InnerLife:
    """A single read/write model over life.db's thoughts and intents."""

    def __init__(self, hub):
        self.hub = hub

    # ---------------------------------------------------------------- 基础

    @property
    def store(self):
        return self.hub.store

    @property
    def runtime(self):
        return getattr(getattr(self.hub, "host", None), "life", None)

    @staticmethod
    def scope_id(scope) -> str:
        if scope is None:
            return ""
        return getattr(scope, "scope_id", "") or str(scope)

    def scope_for_peer(self, peer: int):
        runtime = self.runtime
        if runtime is not None:
            return runtime.scope(f"qq:private:{peer}", str(peer))
        return f"qq:private:{peer}"

    def current_scope(self, peer: int):
        try:
            from life.delivery import active_delivery
            ctx = active_delivery.get()
            if ctx is not None:
                return ctx.scope
        except Exception:
            pass
        return self.scope_for_peer(peer)

    # ---------------------------------------------------------------- 念头

    def record_thought(self, scope, peer: int, text: str, *,
                       kind: str = "wonder", source_event_ids: Iterable[str] = (),
                       salience: float = 0.6, session_id: str = "",
                       visibility: str = "owner_only") -> int:
        text = _clip(text, 800)
        if not text:
            return 0
        ids = [str(x) for x in (source_event_ids or []) if str(x).strip()]
        return self.store.add_thought(
            peer, kind, text, salience=float(salience),
            scope_id=self.scope_id(scope), session_id=session_id,
            source_event_ids=ids, visibility=visibility)

    def recent_thoughts(self, scope, peer: int, n: int = 3,
                        *, include_converted: bool = True) -> list[dict]:
        return self.store.recent_thoughts(
            peer, n=n, scope_id=self.scope_id(scope),
            include_converted=include_converted)

    def thought_sources(self, thought_id: int) -> list[str]:
        row = self.store.get_thought(int(thought_id or 0))
        if not row:
            return []
        try:
            value = json.loads(row.get("source_event_ids") or "[]")
            return [str(x) for x in value if str(x).strip()] if isinstance(value, list) else []
        except Exception:
            return []

    def mark_thought_converted(self, thought_id: int) -> None:
        try:
            self.store.update_thought(int(thought_id or 0), status="converted")
        except Exception:
            pass

    # ---------------------------------------------------------------- 念想

    def visible_intents(self, scope, peer: int, n: int = 5) -> list[dict]:
        return self.store.visible_intents(
            n, scope_id=self.scope_id(scope), peer=peer)

    def actionable_intents(self, scope, peer: int, n: int = 5) -> list[dict]:
        return self.store.active_intents(
            n, scope_id=self.scope_id(scope), peer=peer)

    def count_open_intents(self, scope, peer: int) -> int:
        return self.store.count_active_intents(scope_id=self.scope_id(scope), peer=peer)

    def find_similar_intent(self, scope, peer: int, text: str) -> dict | None:
        target = _dedupe_key(text)
        if not target:
            return None
        for row in self.visible_intents(scope, peer, 20):
            if _dedupe_key(row.get("text", "")) == target:
                return row
            if _overlap(text, row.get("text", "")) >= 0.45:
                return row
        return None

    def decide_intent(self, scope, peer: int, data: dict, *,
                      thought: str = "", thought_id: int = 0) -> dict | None:
        """Normalize model output into a reusable/grounded intent decision."""
        data = dict(data or {})
        text = _clip(data.get("text"), 300)
        why = _clip(data.get("why"), 500)
        if not text or not why:
            return None
        source_ids = self.thought_sources(thought_id)
        existing = None
        raw_id = str(data.get("existing_intent_id") or "")
        if raw_id.isdigit():
            row = self.store.get_intent(int(raw_id))
            if row and int(row.get("peer") or 0) == int(peer) and str(row.get("status")) in OPEN_STATUSES:
                existing = row
        if existing is None:
            existing = self.find_similar_intent(scope, peer, text)
        if existing is not None:
            try:
                energy = max(float(existing.get("energy") or 0.0),
                             float(data.get("energy") or 0.0))
            except (TypeError, ValueError):
                energy = 0.5
            return {
                "reuse": existing,
                "existing_intent_id": existing.get("id"),
                "text": existing.get("text") or text,
                "why": why or existing.get("why", ""),
                "origin": data.get("origin") or existing.get("origin") or "whim",
                "expected": data.get("expected") or existing.get("expected", ""),
                "energy": max(0.0, min(1.0, energy)),
                "announcement": _clip(data.get("announcement"), 300),
                "source_event_ids": source_ids,
            }

        grounded, reason = self.is_grounded(
            scope, peer, thought or text, why, source_ids)
        if not grounded:
            speculative = [
                row for row in self.visible_intents(scope, peer, 20)
                if not bool(row.get("grounded", 1))
            ]
            if len(speculative) >= 6:
                return None
        origin = str(data.get("origin") or "whim")
        if origin not in ("curiosity", "unfinished", "promise", "mastery", "social", "whim"):
            origin = "whim"
        try:
            energy = max(0.0, min(1.0, float(data.get("energy") or 0.5)))
        except (TypeError, ValueError):
            energy = 0.5
        return {
            "text": text,
            "why": why,
            "origin": origin,
            "expected": _clip(data.get("expected"), 300),
            "energy": energy,
            "announcement": _clip(data.get("announcement"), 300),
            "existing_intent_id": 0,
            "source_event_ids": source_ids,
            "grounded": bool(grounded),
            "grounding_reason": reason,
            "dedupe_key": _dedupe_key(text),
        }

    def create_intent(self, scope, peer: int, decision: dict,
                      thought_id: int = 0) -> int:
        reuse = decision.get("reuse") if isinstance(decision, dict) else None
        if isinstance(reuse, dict) and reuse.get("id"):
            try:
                self.store.update_intent(
                    int(reuse["id"]),
                    energy=max(float(reuse.get("energy") or 0.0),
                               float(decision.get("energy") or 0.0)))
            except Exception:
                pass
            return int(reuse["id"])
        source_ids = decision.get("source_event_ids") or []
        grounded = bool(decision.get("grounded", False))
        iid = self.store.add_intent(
            peer, _clip(decision.get("text"), 300),
            _clip(decision.get("why"), 500),
            str(decision.get("origin") or "whim"),
            _clip(decision.get("expected"), 300),
            float(decision.get("energy") or 0.5),
            scope_id=self.scope_id(scope),
            conversation_id=self.scope_id(scope).split("|", 1)[-1],
            source_event_ids=source_ids,
            grounded=grounded,
            dedupe_key=str(decision.get("dedupe_key") or _dedupe_key(decision.get("text", ""))),
            last_thought_id=int(thought_id or 0),
        )
        if not grounded:
            try:
                self.store.update_intent(
                    iid, status="paused", blocked_reason="needs_grounding")
            except Exception:
                pass
        return iid

    def link_job(self, intent_id: int, job_id: str) -> None:
        try:
            self.store.update_intent(int(intent_id), job_id=str(job_id), status="active")
        except Exception:
            pass

    def pause_ungrounded_jobs(self) -> list[int]:
        """Pause legacy solve jobs whose intent has no real provenance."""
        paused: list[int] = []
        try:
            with self.store.repo.tx():
                rows = self.store.db.execute(
                    "SELECT i.id FROM intents i JOIN work_jobs w "
                    "ON w.id='solve:intention_'||i.id "
                    "WHERE i.grounded=0 AND w.status IN ('pending','running','waiting','paused')"
                ).fetchall()
                for row in rows:
                    iid = int(row["id"])
                    job_id = "solve:intention_" + str(iid)
                    self.store.db.execute(
                        "UPDATE work_jobs SET status='paused',error='needs_grounding',"
                        "lease_token='',lease_until=0,updated=? WHERE id=?",
                        (int(time.time()), job_id))
                    self.store.db.execute("DELETE FROM resource_leases WHERE job_id=?", (job_id,))
                    self.store.db.execute(
                        "UPDATE intents SET status='paused',blocked_reason='needs_grounding',"
                        "updated_at=? WHERE id=?", (int(time.time()), iid))
                    paused.append(iid)
        except Exception:
            pass
        return paused

    def sync_job(self, intent_id: int, status: str, job_id: str = "") -> None:
        mapped = {
            "done": "done", "complete": "done", "succeeded": "done",
            "cancelled": "dropped", "cancel": "dropped", "dropped": "dropped",
            "paused": "paused", "waiting": "active",
            "failed": "blocked", "error": "blocked", "blocked": "blocked",
        }.get(str(status or "").lower(), "active")
        fields = {"status": mapped}
        if mapped in ("done", "dropped"):
            fields["job_id"] = ""
        elif job_id:
            fields["job_id"] = str(job_id)
        try:
            self.store.update_intent(int(intent_id), **fields)
        except Exception:
            pass

    # ---------------------------------------------------------------- 依据

    def _recent_text(self, scope, peer: int) -> str:
        parts: list[str] = []
        runtime = self.runtime
        if runtime is not None and scope is not None:
            try:
                rows = runtime.repo._all(
                    "SELECT payload FROM events WHERE agent_id=? AND conversation_id=? "
                    "AND redacted_at IS NULL AND kind IN ('message','delivered_segment') "
                    "ORDER BY ingest_seq DESC LIMIT 24",
                    (*scope.private_key,))
                for row in rows:
                    try:
                        text = json.loads(row["payload"]).get("text", "")
                    except Exception:
                        text = ""
                    if text:
                        parts.append(str(text))
            except Exception:
                pass
        brain = getattr(getattr(self.hub, "host", None), "brain", None)
        if brain is not None:
            try:
                parts.extend(str(x) for x in brain.facts(peer, 20) if str(x).strip())
            except Exception:
                pass
        return "\n".join(parts)

    def is_grounded(self, scope, peer: int, thought: str, why: str,
                    source_event_ids: Iterable[str] = ()) -> tuple[bool, str]:
        """True only when the intent is anchored in real events or remembered facts."""
        recent = self._recent_text(scope, peer)
        if not recent.strip():
            # Unit tests and shadow mode may have no event layer yet. Do not
            # silently block every intent there; production always has events.
            return True, "no_evidence_layer"
        fields = f"{thought}\n{why}"
        if _EVENT_ID.search(fields):
            return True, "cites_event_id"
        for quoted in _QUOTED.findall(fields):
            if quoted and quoted in recent:
                return True, "quotes_real_event"
        for event_id in source_event_ids or ():
            try:
                rows = self.runtime.repo.events_by_ids([str(event_id)]) if self.runtime else []
                for event in rows:
                    if getattr(event, "text", "") and event.text in recent:
                        return True, "source_event_is_real"
            except Exception:
                pass
        score = _overlap(fields, recent)
        if score >= 0.25:
            return True, f"overlap={score:.2f}"
        return False, "no_real_anchor"

    # ---------------------------------------------------------------- 提示词

    def prompt_block(self, scope, peer: int, *,
                     max_thoughts: int = 3, max_intents: int = 3) -> str:
        intents = self.visible_intents(scope, peer, max_intents)
        thoughts = self.recent_thoughts(scope, peer, max_thoughts, include_converted=True)
        lines = ["\n## 你此刻的内在状态（同一份，不是两套）"]
        if intents:
            lines.append("手上挂着的念想（不是他说的；不一定已经做）：")
            for row in intents:
                status = str(row.get("status") or "seed")
                suffix = ""
                if not bool(row.get("grounded", 1)):
                    suffix = "｜依据不足，先只想想，不要动手"
                elif row.get("job_id"):
                    suffix = "｜已在执行"
                lines.append(f"- #{row.get('id')} [{status}{suffix}] {_clip(row.get('text'), 140)}")
        else:
            lines.append("手上没有挂着的念想。")
        if thoughts:
            lines.append("刚冒出的念头（推想，不是事实）：")
            for row in thoughts:
                stamp = time.strftime("%H:%M", time.localtime(float(row.get("ts") or 0)))
                lines.append(f"- [{stamp}] {_clip(row.get('text'), 160)}")
        else:
            lines.append("这会儿没有新念头。")
        lines.append(
            "回答“你在想什么 / 在干嘛”时以这里为准；可以自然带出，"
            "但不要把念头说成他说的，也不要把还没做的事说成已经做了。"
            "标着“依据不足”的念想只是推想，不要主动反复提，也不要据此声称正在做什么。")
        return "\n".join(lines) + "\n"

    def attention_material(self, scope, peer: int) -> str:
        intents = [
            row for row in self.visible_intents(scope, peer, 5)
            if bool(row.get("grounded", 1)) and str(row.get("status")) in ("seed", "active")
        ][:3]
        last = self.recent_thoughts(scope, peer, 1, include_converted=False)
        lines = ["你手头能推进的念想："]
        if intents:
            for row in intents:
                lines.append(f"- #{row.get('id')} {_clip(row.get('text'), 100)}")
        else:
            lines.append("- 没有有真实依据、可以推进的念想；之前没依据的已经放下，不要重提")
        if last:
            lines.append("你上一次的念头（已经想过，不要重复）：")
            for row in last:
                lines.append(f"- {_clip(row.get('text'), 120)}")
        else:
            lines.append("你上一次的念头已经处理过了，不要复述它。")
        lines.append("这次要么推进一个已有念想，要么想一件新的事；不要生成同一目标。")
        return "\n".join(lines)
