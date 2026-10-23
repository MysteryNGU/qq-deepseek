#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""life.db 的短事务读写层。

约定：
- 所有写操作都在 `with repo.tx():` 里；模型/网络调用永远在事务外。
- 不在这一层写角色台词或心理规则，只做存储、约束和幂等。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from . import schema
from .contracts import (
    Event, Memory, MemorySource, OpenLoop, Scope, Turn, new_id, now_ts,
)


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class Repository:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._tx_depth = 0
        self._version_floors = {}
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self._lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA busy_timeout=5000")
            self.db.execute("PRAGMA foreign_keys=ON")
            schema.init(self.db)

    def close(self) -> None:
        with self._lock:
            try:
                self.db.close()
            except Exception:
                pass

    @contextmanager
    def tx(self) -> Iterator["Repository"]:
        with self._lock:
            depth = self._tx_depth
            savepoint = f"life_tx_{depth}"
            self.db.execute("BEGIN IMMEDIATE" if depth == 0 else f"SAVEPOINT {savepoint}")
            self._tx_depth += 1
            try:
                yield self
            except BaseException:
                if depth == 0:
                    self.db.rollback()
                else:
                    self.db.execute(f"ROLLBACK TO {savepoint}")
                    self.db.execute(f"RELEASE {savepoint}")
                raise
            else:
                if depth == 0:
                    self.db.commit()
                else:
                    self.db.execute(f"RELEASE {savepoint}")
            finally:
                self._tx_depth -= 1

    def _exec(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Cursor:
        return self.db.execute(sql, tuple(args))

    def _one(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.db.execute(sql, tuple(args)).fetchone()

    def _all(self, sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.db.execute(sql, tuple(args)).fetchall()

    # ---------------------------------------------------------------- 事件

    def _next_seq(self, agent_id: str) -> int:
        key = f"seq:{agent_id}"
        row = self._one("SELECT v FROM meta WHERE k=?", (key,))
        seq = int(row["v"]) + 1 if row else 1
        self._exec("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (key, str(seq)))
        return seq

    def ingest_event(self, event: Event) -> tuple[str, bool]:
        """幂等入库：同一外部消息 ID 只产生一条事件。"""
        with self.tx():
            if event.external_id:
                row = self._one(
                    "SELECT id FROM events WHERE agent_id=? AND conversation_id=? AND external_id=?",
                    (event.scope.agent_id, event.scope.conversation_id, event.external_id),
                )
                if row:
                    return str(row["id"]), False
            seq = event.ingest_seq or self._next_seq(event.scope.agent_id)
            self._exec(
                "INSERT INTO events(id,agent_id,conversation_id,participant_id,"
                "audience_policy_id,kind,source_kind,external_id,occurred_at,received_at,"
                "ingest_seq,actor_id,payload,source_event_ids,schema_version,redacted_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    event.id, event.scope.agent_id, event.scope.conversation_id,
                    event.scope.participant_id, event.scope.audience_policy_id,
                    event.kind, event.source_kind, event.external_id,
                    float(event.occurred_at), float(event.received_at), int(seq),
                    event.actor_id, _dumps(event.payload), _dumps(event.source_event_ids),
                    int(event.schema_version), event.redacted_at,
                ),
            )
            return event.id, True

    def get_event(self, event_id: str) -> Event | None:
        row = self._one("SELECT * FROM events WHERE id=?", (event_id,))
        return self._event(row) if row else None

    def events_after(self, scope: Scope, after_seq: int, limit: int = 200) -> list[Event]:
        rows = self._all(
            "SELECT * FROM events WHERE agent_id=? AND conversation_id=? AND ingest_seq>? "
            "AND redacted_at IS NULL ORDER BY ingest_seq LIMIT ?",
            (scope.agent_id, scope.conversation_id, int(after_seq), int(limit)),
        )
        return [self._event(r) for r in rows]

    def events_by_ids(self, event_ids: Iterable[str]) -> list[Event]:
        ids = [str(x) for x in event_ids]
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        rows = self._all(
            f"SELECT * FROM events WHERE id IN ({marks}) AND redacted_at IS NULL "
            "ORDER BY ingest_seq", ids)
        return [self._event(r) for r in rows]

    def max_ingest_seq(self, scope: Scope) -> int:
        row = self._one(
            "SELECT COALESCE(MAX(ingest_seq),0) AS m FROM events "
            "WHERE agent_id=? AND conversation_id=?",
            (scope.agent_id, scope.conversation_id),
        )
        return int(row["m"]) if row else 0

    @staticmethod
    def _event(row: sqlite3.Row) -> Event:
        return Event(
            id=row["id"],
            scope=Scope(row["agent_id"], row["conversation_id"],
                        row["participant_id"], row["audience_policy_id"]),
            kind=row["kind"], source_kind=row["source_kind"],
            payload=_loads(row["payload"], {}),
            occurred_at=float(row["occurred_at"]), received_at=float(row["received_at"]),
            external_id=row["external_id"], ingest_seq=int(row["ingest_seq"]),
            actor_id=row["actor_id"],
            source_event_ids=_loads(row["source_event_ids"], []),
            schema_version=int(row["schema_version"]),
            redacted_at=row["redacted_at"],
        )

    # ---------------------------------------------------------------- 轮次

    def create_turn(self, scope: Scope, origin: str, event_ids: list[str]) -> Turn:
        with self.tx():
            for eid in event_ids:
                row = self._one("SELECT turn_id FROM turn_inputs WHERE event_id=?", (eid,))
                if row:
                    got = self.get_turn(str(row["turn_id"]))
                    if got:
                        return got
            turn = Turn(id=new_id("turn"), scope=scope, origin=origin,
                        input_event_ids=list(event_ids))
            self._exec(
                "INSERT INTO turns(id,agent_id,conversation_id,participant_id,origin,status,"
                "generation,context_revision,policy_epoch,forget_epoch,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (turn.id, scope.agent_id, scope.conversation_id, scope.participant_id,
                 origin, turn.status, turn.generation, turn.context_revision,
                 turn.policy_epoch, self.forget_epoch(scope.scope_id), turn.created_at),
            )
            for eid in event_ids:
                self._exec(
                    "INSERT OR IGNORE INTO turn_inputs(turn_id,event_id,added_at) VALUES(?,?,?)",
                    (turn.id, eid, now_ts()),
                )
            return turn

    def get_turn(self, turn_id: str) -> Turn | None:
        row = self._one("SELECT * FROM turns WHERE id=?", (turn_id,))
        if not row:
            return None
        inputs = [r["event_id"] for r in self._all(
            "SELECT event_id FROM turn_inputs WHERE turn_id=? ORDER BY added_at", (turn_id,))]
        return Turn(
            id=row["id"],
            scope=Scope(row["agent_id"], row["conversation_id"], row["participant_id"]),
            origin=row["origin"], input_event_ids=inputs, status=row["status"],
            generation=int(row["generation"]), lease_owner=row["lease_owner"],
            lease_until=row["lease_until"], context_revision=int(row["context_revision"]),
            policy_epoch=int(row["policy_epoch"]), forget_epoch=int(row["forget_epoch"]),
            created_at=float(row["created_at"]),
        )

    def set_turn_status(self, turn_id: str, status: str) -> None:
        with self.tx():
            self._exec("UPDATE turns SET status=? WHERE id=?", (status, turn_id))

    # ---------------------------------------------------------------- 效果去重

    def claim_effect(self, input_event_id: str, effect_kind: str, aggregate_id: str) -> bool:
        """返回 True 表示这次真的应用；False 表示之前已经应用过。"""
        with self.tx():
            cur = self._exec(
                "INSERT OR IGNORE INTO applied_effects(input_event_id,effect_kind,aggregate_id,applied_at) "
                "VALUES(?,?,?,?)",
                (input_event_id, effect_kind, aggregate_id, now_ts()),
            )
            return cur.rowcount == 1

    # ---------------------------------------------------------------- 状态快照

    def load_state(self, aggregate_id: str) -> tuple[int, dict[str, Any], int] | None:
        row = self._one(
            "SELECT version,payload,last_applied_seq FROM state_snapshots "
            "WHERE aggregate_id=? ORDER BY version DESC LIMIT 1", (aggregate_id,))
        if not row:
            return None
        return int(row["version"]), _loads(row["payload"], {}), int(row["last_applied_seq"])

    def save_state(self, aggregate_id: str, payload: dict[str, Any],
                   last_applied_seq: int = 0) -> int:
        with self.tx():
            self._validate_state(aggregate_id,payload)
            row = self._one(
                "SELECT COALESCE(MAX(version),0) AS v FROM state_snapshots WHERE aggregate_id=?",
                (aggregate_id,))
            version = max(int(row["v"]) if row else 0,self._version_floors.get(aggregate_id,0))+1
            self._exec(
                "INSERT INTO state_snapshots(aggregate_id,version,last_applied_seq,payload,updated_at) "
                "VALUES(?,?,?,?,?)",
                (aggregate_id, version, int(last_applied_seq), _dumps(payload), now_ts()),
            )
            return version

    def save_state_cas(self, aggregate_id: str, payload: dict[str, Any],
                       expected_version: int, last_applied_seq: int = 0) -> int | None:
        """版本 CAS 提交；返回新版本；冲突返回 None，不覆盖别人的更新。"""
        with self.tx():
            self._validate_state(aggregate_id,payload)
            row = self._one(
                "SELECT COALESCE(MAX(version),0) AS v FROM state_snapshots WHERE aggregate_id=?",
                (aggregate_id,))
            current = int(row["v"]) if row else 0
            if int(expected_version) != current:
                return None
            version = max(current,self._version_floors.get(aggregate_id,0))+1
            self._exec(
                "INSERT INTO state_snapshots(aggregate_id,version,last_applied_seq,payload,updated_at) "
                "VALUES(?,?,?,?,?)",
                (aggregate_id, version, int(last_applied_seq), _dumps(payload), now_ts()),
            )
            return version

    def _validate_state(self,key,payload):
        validator = getattr(self,'relationship_validator',None)
        if key.startswith('relationship:') and validator:
            validator(key,payload)

    # ---------------------------------------------------------------- 驱力结算

    def record_drive_settlement(self, *, scope_id: str, root_id: str, dimension: str,
                                effect_kind: str, cycle_id: int | str, before_value: float,
                                after_value: float, source_ids: list[str] | None = None,
                                payload: dict[str, Any] | None = None,
                                rule_version: str = "drive-v2") -> bool:
        """幂等写入一次驱力效果；返回 True 表示本次第一次领取。"""
        key = str(scope_id) + "|" + str(root_id) + "|" + str(dimension) + "|" + str(effect_kind) + "|" + str(cycle_id)
        with self.tx():
            cur = self._exec(
                "INSERT OR IGNORE INTO drive_settlements(id,scope_id,root_id,dimension,effect_kind,"
                "cycle_id,before_value,after_value,source_ids,payload,rule_version,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                ("drv_" + __import__("hashlib").sha256(key.encode()).hexdigest()[:24],
                 str(scope_id), str(root_id), str(dimension), str(effect_kind),
                 str(cycle_id or ""), float(before_value), float(after_value),
                 _dumps(list(source_ids or [])), _dumps(payload or {}),
                 str(rule_version), now_ts()),
            )
            return cur.rowcount == 1

    def drive_settlement_by_root(self, *, scope_id: str, root_id: str,
                                 effect_kind: str) -> bool:
        row = self._one(
            "SELECT 1 FROM drive_settlements WHERE scope_id=? AND root_id=? AND effect_kind=? "
            "AND revoked_at IS NULL LIMIT 1",
            (str(scope_id), str(root_id), str(effect_kind)),
        )
        return row is not None

    def drive_settlement_exists(self, *, scope_id: str, root_id: str, dimension: str,
                                effect_kind: str, cycle_id: int | str = "") -> bool:
        row = self._one(
            "SELECT 1 FROM drive_settlements WHERE scope_id=? AND root_id=? AND dimension=? "
            "AND effect_kind=? AND cycle_id=? AND revoked_at IS NULL LIMIT 1",
            (str(scope_id), str(root_id), str(dimension), str(effect_kind), str(cycle_id or "")),
        )
        return row is not None

    def revoke_drive_settlements(self, *, scope_id: str, source_ids: list[str] | None = None,
                                 root_ids: list[str] | None = None) -> int:
        roots = [str(x) for x in (root_ids or []) if str(x)]
        sources = [str(x) for x in (source_ids or []) if str(x)]
        with self.tx():
            if roots:
                marks = ",".join("?" for _ in roots)
                cur = self._exec(
                    f"UPDATE drive_settlements SET revoked_at=? WHERE scope_id=? AND revoked_at IS NULL "
                    f"AND root_id IN ({marks})", (now_ts(), str(scope_id), *roots))
                return int(cur.rowcount)
            if sources:
                rows = self._all(
                    "SELECT id,source_ids FROM drive_settlements WHERE scope_id=? AND revoked_at IS NULL",
                    (str(scope_id),))
                revoked = 0
                for row in rows:
                    refs = set(_loads(row["source_ids"], []))
                    if refs & set(sources):
                        self._exec("UPDATE drive_settlements SET revoked_at=? WHERE id=?",
                                   (now_ts(), row["id"]))
                        revoked += 1
                return revoked
            return 0

    def drive_settlements(self, scope_id: str, limit: int = 200) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT * FROM drive_settlements WHERE scope_id=? ORDER BY created_at DESC LIMIT ?",
            (str(scope_id), int(limit)))
        out = []
        for row in rows:
            item = dict(row)
            item["source_ids"] = _loads(item.get("source_ids"), [])
            item["payload"] = _loads(item.get("payload"), {})
            out.append(item)
        return out

    # ---------------------------------------------------------------- 记忆

    def insert_memory(self, memory: Memory, terms: list[str]) -> str:
        with self.tx():
            self._exec(
                "INSERT INTO memories(id,agent_id,conversation_id,audience_policy_id,kind,"
                "subject_id,predicate,normalized_value,summary,event_group_id,topic_tags,"
                "source_kind,status,valid_from,valid_until,time_precision,recorded_at,updated_at,"
                "version,salience,source_quality,uncertainty_reason,supersedes_id,derived_from,"
                "last_mentioned_at,mention_count_window,retention_policy_id,forget_epoch) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    memory.id, memory.scope.agent_id, memory.scope.conversation_id,
                    memory.scope.audience_policy_id, memory.kind, memory.subject_id,
                    memory.predicate, memory.normalized_value, memory.summary,
                    memory.event_group_id, _dumps(memory.topic_tags), memory.source_kind,
                    memory.status, memory.valid_from, memory.valid_until, memory.time_precision,
                    memory.recorded_at, memory.updated_at, memory.version, memory.salience,
                    memory.source_quality, memory.uncertainty_reason, memory.supersedes_id,
                    _dumps(memory.derived_from), memory.last_mentioned_at,
                    memory.mention_count_window, memory.retention_policy_id, memory.forget_epoch,
                ),
            )
            for src in memory.sources:
                self._exec(
                    "INSERT OR IGNORE INTO memory_sources(memory_id,event_id,relation,quote_start,quote_end) "
                    "VALUES(?,?,?,?,?)",
                    (memory.id, src.event_id, src.relation, src.quote_start, src.quote_end),
                )
            self._set_terms(memory.id, terms)
            return memory.id

    def _set_terms(self, memory_id: str, terms: list[str]) -> None:
        self._exec("DELETE FROM memory_terms WHERE memory_id=?", (memory_id,))
        for term in dict.fromkeys(terms):
            if term:
                self._exec("INSERT OR IGNORE INTO memory_terms(memory_id,term) VALUES(?,?)",
                           (memory_id, term))

    def update_memory(self, memory_id: str, **fields: Any) -> None:
        allowed = {
            "summary", "status", "valid_until", "updated_at", "salience", "source_quality",
            "uncertainty_reason", "supersedes_id", "last_mentioned_at",
            "mention_count_window", "forget_epoch", "normalized_value", "version",
        }
        sets, args = [], []
        for key, value in fields.items():
            if key not in allowed:
                continue
            sets.append(f"{key}=?")
            args.append(value)
        if not sets:
            return
        args.append(memory_id)
        with self.tx():
            self._exec(f"UPDATE memories SET {', '.join(sets)} WHERE id=?", args)

    def get_memory(self, memory_id: str) -> Memory | None:
        row = self._one("SELECT * FROM memories WHERE id=?", (memory_id,))
        if not row:
            return None
        sources = [
            MemorySource(r["memory_id"], r["event_id"], r["relation"],
                         r["quote_start"], r["quote_end"])
            for r in self._all("SELECT * FROM memory_sources WHERE memory_id=?", (memory_id,))
        ]
        return self._memory(row, sources)

    def memories_by_ids(self, memory_ids: Iterable[str]) -> list[Memory]:
        ids = [str(x) for x in memory_ids]
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        rows = self._all(f"SELECT * FROM memories WHERE id IN ({marks})", ids)
        return self._attach_sources([self._memory(r) for r in rows])

    def search_memories(self, scope: Scope, terms: list[str],
                        statuses: Iterable[str], limit: int = 40) -> list[Memory]:
        terms = [t for t in dict.fromkeys(terms) if t]
        statuses = list(statuses)
        if not terms or not statuses:
            return []
        tmarks = ",".join("?" for _ in terms)
        smarks = ",".join("?" for _ in statuses)
        rows = self._all(
            f"SELECT m.*, COUNT(*) AS hit FROM memories m "
            f"JOIN memory_terms t ON t.memory_id=m.id "
            f"WHERE m.agent_id=? AND m.conversation_id=? AND m.status IN ({smarks}) "
            f"AND t.term IN ({tmarks}) AND m.forget_epoch<=? "
            f"GROUP BY m.id ORDER BY hit DESC, m.updated_at DESC LIMIT ?",
            [scope.agent_id, scope.conversation_id, *statuses, *terms,
             self.forget_epoch(scope.scope_id), int(limit)],
        )
        return self._attach_sources([self._memory(r) for r in rows])

    def recent_memories(self, scope: Scope, statuses: Iterable[str],
                        limit: int = 30) -> list[Memory]:
        statuses = list(statuses)
        if not statuses:
            return []
        smarks = ",".join("?" for _ in statuses)
        rows = self._all(
            f"SELECT * FROM memories WHERE agent_id=? AND conversation_id=? "
            f"AND status IN ({smarks}) ORDER BY updated_at DESC LIMIT ?",
            [scope.agent_id, scope.conversation_id, *statuses, int(limit)],
        )
        return self._attach_sources([self._memory(r) for r in rows])

    def find_facts(self, scope: Scope, subject_id: str, predicate: str,
                   statuses: Iterable[str] = ("active", "disputed")) -> list[Memory]:
        statuses = list(statuses)
        smarks = ",".join("?" for _ in statuses)
        rows = self._all(
            f"SELECT * FROM memories WHERE agent_id=? AND conversation_id=? "
            f"AND kind='fact' AND subject_id=? AND predicate=? AND status IN ({smarks}) "
            f"ORDER BY recorded_at DESC",
            [scope.agent_id, scope.conversation_id, subject_id, predicate, *statuses],
        )
        return self._attach_sources([self._memory(r) for r in rows])

    def add_memory_source(self, memory_id: str, source: MemorySource) -> None:
        with self.tx():
            self._exec(
                "INSERT OR IGNORE INTO memory_sources(memory_id,event_id,relation,quote_start,quote_end) "
                "VALUES(?,?,?,?,?)",
                (memory_id, source.event_id, source.relation,
                 source.quote_start, source.quote_end),
            )

    def memory_terms(self, memory_id: str) -> list[str]:
        return [r["term"] for r in self._all(
            "SELECT term FROM memory_terms WHERE memory_id=?", (memory_id,))]

    def _attach_sources(self, memories: list[Memory]) -> list[Memory]:
        ids = [m.id for m in memories]
        if not ids:
            return memories
        marks = ",".join("?" for _ in ids)
        rows = self._all(
            f"SELECT * FROM memory_sources WHERE memory_id IN ({marks})", ids)
        by_id: dict[str, list[MemorySource]] = {}
        for r in rows:
            by_id.setdefault(r["memory_id"], []).append(
                MemorySource(r["memory_id"], r["event_id"], r["relation"],
                             r["quote_start"], r["quote_end"]))
        for m in memories:
            m.sources = by_id.get(m.id, [])
        return memories

    @staticmethod
    def _memory(row: sqlite3.Row, sources: list[MemorySource] | None = None) -> Memory:
        return Memory(
            id=row["id"],
            scope=Scope(row["agent_id"], row["conversation_id"], "",
                        row["audience_policy_id"]),
            kind=row["kind"], summary=row["summary"], source_kind=row["source_kind"],
            status=row["status"], subject_id=row["subject_id"],
            predicate=row["predicate"], normalized_value=row["normalized_value"],
            event_group_id=row["event_group_id"],
            topic_tags=_loads(row["topic_tags"], []),
            valid_from=row["valid_from"], valid_until=row["valid_until"],
            time_precision=row["time_precision"], recorded_at=float(row["recorded_at"]),
            updated_at=float(row["updated_at"]), version=int(row["version"]),
            salience=float(row["salience"]), source_quality=float(row["source_quality"]),
            uncertainty_reason=row["uncertainty_reason"], supersedes_id=row["supersedes_id"],
            derived_from=_loads(row["derived_from"], []),
            last_mentioned_at=row["last_mentioned_at"],
            mention_count_window=int(row["mention_count_window"]),
            retention_policy_id=row["retention_policy_id"],
            forget_epoch=int(row["forget_epoch"]),
            sources=sources or [],
        )

    # ---------------------------------------------------------------- 未完成事项

    def insert_open_loop(self, loop: OpenLoop) -> str:
        with self.tx():
            from .tasks import TaskService
            tasks = TaskService(self)
            ref = 'task_' + loop.source_event_ids[0] if loop.source_event_ids else 'loop_' + loop.id
            existing = tasks.get(ref, loop.scope.scope_id) or {}
            tasks.ensure(loop.scope, ref, loop.summary,
                         loop_ids=list(dict.fromkeys(existing.get('loop_ids', [])+[loop.id])))
            self._exec(
                "INSERT INTO open_loops(id,agent_id,conversation_id,kind,summary,source_event_ids,"
                "expected_outcome,acceptance_checks,status,next_step,checkpoint,waiting_for,"
                "due_at,next_review_at,retry_after,permission_scope_id,result_evidence_ids,version,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    loop.id, loop.scope.agent_id, loop.scope.conversation_id, loop.kind,
                    loop.summary, _dumps(loop.source_event_ids), loop.expected_outcome,
                    _dumps(loop.acceptance_checks), loop.status, loop.next_step,
                    loop.checkpoint, loop.waiting_for, loop.due_at, loop.next_review_at,
                    loop.retry_after, loop.permission_scope_id,
                    _dumps(loop.result_evidence_ids), loop.version, loop.created_at,
                    loop.updated_at,
                ),
            )
            return loop.id

    def active_open_loops(self, scope: Scope, limit: int = 20) -> list[OpenLoop]:
        rows = self._all(
            "SELECT * FROM open_loops WHERE agent_id=? AND conversation_id=? "
            "AND status NOT IN ('retired','cancelled','done','succeeded') "
            "ORDER BY COALESCE(next_review_at, due_at, created_at) LIMIT ?",
            (scope.agent_id, scope.conversation_id, int(limit)),
        )
        return [self._loop(r) for r in rows]

    @staticmethod
    def _loop(row: sqlite3.Row) -> OpenLoop:
        return OpenLoop(
            id=row["id"],
            scope=Scope(row["agent_id"], row["conversation_id"]),
            kind=row["kind"], summary=row["summary"],
            source_event_ids=_loads(row["source_event_ids"], []),
            expected_outcome=row["expected_outcome"],
            acceptance_checks=_loads(row["acceptance_checks"], []),
            status=row["status"], next_step=row["next_step"],
            checkpoint=row["checkpoint"], waiting_for=row["waiting_for"],
            due_at=row["due_at"], next_review_at=row["next_review_at"],
            retry_after=row["retry_after"],
            permission_scope_id=row["permission_scope_id"],
            result_evidence_ids=_loads(row["result_evidence_ids"], []),
            version=int(row["version"]), created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    # ---------------------------------------------------------------- 投影批次 / 日记作业

    def batch_committed(self, scope_id: str, projector_kind: str, batch_id: str) -> bool:
        return self._one(
            "SELECT 1 FROM projection_batches WHERE scope_id=? AND projector_kind=? AND batch_id=?",
            (scope_id, projector_kind, batch_id),
        ) is not None

    def mark_batch_committed(self, scope_id: str, projector_kind: str, batch_id: str) -> None:
        with self.tx():
            self._exec(
                "INSERT OR IGNORE INTO projection_batches(scope_id,projector_kind,batch_id,status,committed_at) "
                "VALUES(?,?,?,'committed',?)",
                (scope_id, projector_kind, batch_id, now_ts()),
            )

    def ensure_daily_job(self, scope_id: str, local_date: str, job_kind: str,
                         version: int = 1, due_at: float | None = None) -> None:
        with self.tx():
            self._exec(
                "INSERT OR IGNORE INTO daily_jobs(scope_id,local_date,job_kind,version,status,due_at,updated_at) "
                "VALUES(?,?,?,?,'pending',?,?)",
                (scope_id, local_date, job_kind, int(version), due_at, now_ts()),
            )

    def claim_daily_job(self, scope_id: str, local_date: str, job_kind: str,
                        version: int = 1, stale_after: int = 600) -> str | None:
        with self.tx():
            now = now_ts()
            lease_token = new_id("lease")
            cur = self._exec(
                "UPDATE daily_jobs SET status='running', attempts=attempts+1, updated_at=?, lease_token=? "
                "WHERE scope_id=? AND local_date=? AND job_kind=? AND version=? "
                "AND (status='pending' OR (status='running' AND updated_at<?))",
                (now, lease_token, scope_id, local_date, job_kind, int(version), now - stale_after),
            )
            return lease_token if cur.rowcount == 1 else None

    def daily_job_is_current(self, scope_id: str, local_date: str, job_kind: str,
                             version: int, lease_token: str) -> bool:
        return self._one(
            "SELECT 1 FROM daily_jobs WHERE scope_id=? AND local_date=? AND job_kind=? "
            "AND version=? AND status='running' AND lease_token=? AND updated_at>=?",
            (scope_id, local_date, job_kind, version, lease_token, now_ts() - 600)) is not None

    def daily_job_status(self, scope_id: str, local_date: str, job_kind: str,
                         version: int = 1) -> str:
        row = self._one(
            "SELECT status FROM daily_jobs WHERE scope_id=? AND local_date=? "
            "AND job_kind=? AND version=?", (scope_id, local_date, job_kind, int(version)))
        return str(row["status"]) if row else ""

    def requeue_daily_job(self, scope_id: str, local_date: str, job_kind: str,
                          version: int = 1) -> None:
        with self.tx():
            self._exec(
                "UPDATE daily_jobs SET status='pending', updated_at=? "
                "WHERE scope_id=? AND local_date=? AND job_kind=? AND version=? "
                "AND (status='failed' OR (status='running' AND updated_at<?))",
                (now_ts(), scope_id, local_date, job_kind, int(version), now_ts() - 600))

    def finish_daily_job(self, scope_id: str, local_date: str, job_kind: str,
                         version: int = 1, error: str = "", *, lease_token: str = "") -> bool:
        with self.tx():
            cur = self._exec(
                "UPDATE daily_jobs SET status=?, last_error=?, updated_at=? "
                "WHERE scope_id=? AND local_date=? AND job_kind=? AND version=? "
                "AND status='running' AND lease_token=? AND updated_at>=?",
                ("failed" if error else "done", error[:500], now_ts(),
                 scope_id, local_date, job_kind, int(version), lease_token, now_ts() - 600),
            )
            return cur.rowcount == 1

    # ---------------------------------------------------------------- 遗忘屏障

    def forget_epoch(self, scope_id: str) -> int:
        row = self._one("SELECT epoch FROM forget_epochs WHERE scope_id=?", (scope_id,))
        return int(row["epoch"]) if row else 0

    def bump_forget_epoch(self, scope_id: str) -> int:
        with self.tx():
            row = self._one("SELECT epoch FROM forget_epochs WHERE scope_id=?", (scope_id,))
            epoch = (int(row["epoch"]) if row else 0) + 1
            self._exec("INSERT OR REPLACE INTO forget_epochs(scope_id,epoch) VALUES(?,?)",
                       (scope_id, epoch))
            return epoch

    def redact_memory(self, memory_id: str, epoch: int) -> None:
        with self.tx():
            self._exec(
                "UPDATE memories SET status='redacted', forget_epoch=?, updated_at=? WHERE id=?",
                (int(epoch), now_ts(), memory_id),
            )
            self._exec("DELETE FROM memory_terms WHERE memory_id=?", (memory_id,))

    # ---------------------------------------------------------------- 决策记录

    def save_decision(self, record: dict[str, Any]) -> str:
        rid = record.get("id") or new_id("dec")
        with self.tx():
            self._exec(
                "INSERT OR REPLACE INTO decision_records(id,turn_id,agent_id,conversation_id,"
                "input_event_ids,memory_ids,appraisal_id,state_version_before,state_version_after,"
                "behavior_plan,action_id,outcome,reason_codes,policy_version,prompt_version,"
                "model_id,tokens,duration_ms,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    rid, record.get("turn_id", ""), record.get("agent_id", ""),
                    record.get("conversation_id", ""),
                    _dumps(record.get("input_event_ids", [])),
                    _dumps(record.get("memory_ids", [])),
                    record.get("appraisal_id", ""),
                    int(record.get("state_version_before", 0)),
                    int(record.get("state_version_after", 0)),
                    _dumps(record.get("behavior_plan", {})),
                    record.get("action_id", ""), record.get("outcome", ""),
                    _dumps(record.get("reason_codes", [])),
                    record.get("policy_version", "life-1"),
                    record.get("prompt_version", ""), record.get("model_id", ""),
                    int(record.get("tokens", 0)), int(record.get("duration_ms", 0)),
                    record.get("created_at") or now_ts(),
                ),
            )
            return rid

    # ---------------------------------------------------------------- 统计

    def stats(self) -> dict[str, int]:
        def count(table: str) -> int:
            row = self._one(f"SELECT COUNT(*) AS c FROM {table}")
            return int(row["c"]) if row else 0
        row = self._one("SELECT COUNT(*) AS c FROM memories WHERE status='active'")
        return {
            "events": count("events"),
            "turns": count("turns"),
            "memories": count("memories"),
            "active_memories": int(row["c"]) if row else 0,
            "open_loops": count("open_loops"),
            "decision_records": count("decision_records"),
            "forget_epochs": count("forget_epochs"),
        }

    def apply_effect_and_save_state(self, input_event_id: str, effect_kind: str,
                                    aggregate_id: str, payload: dict[str, Any],
                                    last_applied_seq: int = 0) -> int | None:
        """同一事务里完成"效果去重 + 状态快照"，避免先占坑后崩溃。

        返回新版本号；若该输入已经应用过同一效果，返回 None。
        """
        with self.tx():
            self._validate_state(aggregate_id,payload)
            cur = self._exec(
                "INSERT OR IGNORE INTO applied_effects(input_event_id,effect_kind,aggregate_id,applied_at) "
                "VALUES(?,?,?,?)",
                (input_event_id, effect_kind, aggregate_id, now_ts()),
            )
            if cur.rowcount != 1:
                return None
            row = self._one(
                "SELECT COALESCE(MAX(version),0) AS v FROM state_snapshots WHERE aggregate_id=?",
                (aggregate_id,))
            version = max(int(row["v"]) if row else 0,self._version_floors.get(aggregate_id,0))+1
            self._exec(
                "INSERT INTO state_snapshots(aggregate_id,version,last_applied_seq,payload,updated_at) "
                "VALUES(?,?,?,?,?)",
                (aggregate_id, version, int(last_applied_seq), _dumps(payload), now_ts()),
            )
            return version

    def get_open_loop(self, loop_id: str) -> OpenLoop | None:
        row = self._one("SELECT * FROM open_loops WHERE id=?", (loop_id,))
        return self._loop(row) if row else None

    def update_open_loop(self, loop_id: str, **fields: Any) -> None:
        status = fields.get('status')
        if status in {'done','succeeded','retired','cancelled','paused'}:
            from .tasks import TaskService
            loop = self.get_open_loop(loop_id)
            if loop:
                ref = 'task_' + loop.source_event_ids[0] if loop.source_event_ids else 'loop_' + loop.id
                service = TaskService(self)
                if service.get(ref, loop.scope.scope_id):
                    service.transition(loop.scope.scope_id, ref,
                        'done' if status in {'done','succeeded','retired'} else status, cause='loop_transition')
        allowed = {
            "summary", "status", "next_step", "checkpoint", "waiting_for",
            "due_at", "next_review_at", "retry_after", "permission_scope_id",
            "result_evidence_ids", "version", "updated_at",
        }
        sets, args = [], []
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key in ("result_evidence_ids",):
                value = _dumps(value)
            sets.append(f"{key}=?")
            args.append(value)
        if not sets:
            return
        args.append(loop_id)
        with self.tx():
            self._exec(f"UPDATE open_loops SET {', '.join(sets)} WHERE id=?", args)

    def due_open_loops(self, scope: Scope, now: float, limit: int = 20) -> list[OpenLoop]:
        rows = self._all(
            "SELECT * FROM open_loops WHERE agent_id=? AND conversation_id=? "
            "AND status IN ('ready','running','waiting_permission','waiting_dependency',"
            "'paused','outcome_unknown','blocked') "
            "AND COALESCE(next_review_at, due_at, created_at)<=? "
            "ORDER BY COALESCE(next_review_at, due_at, created_at) LIMIT ?",
            (scope.agent_id, scope.conversation_id, float(now), int(limit)),
        )
        return [self._loop(r) for r in rows]

    def legacy_mapped(self, source_db: str, table: str, pk: str) -> str | None:
        row = self._one(
            "SELECT target_id FROM legacy_mappings "
            "WHERE source_database_id=? AND table_name=? AND source_primary_key=?",
            (source_db, table, str(pk)),
        )
        return str(row["target_id"]) if row else None

    def record_legacy_mapping(self, source_db: str, table: str, pk: str,
                              target_kind: str, target_id: str) -> None:
        with self.tx():
            self._exec(
                "INSERT OR IGNORE INTO legacy_mappings("
                "source_database_id,table_name,source_primary_key,target_kind,target_id,migrated_at) "
                "VALUES(?,?,?,?,?,?)",
                (source_db, table, str(pk), target_kind, target_id, now_ts()),
            )

    def events_between(self, scope: Scope, start_ts: float, end_ts: float,
                       limit: int = 400) -> list[Event]:
        rows = self._all(
            "SELECT * FROM events WHERE agent_id=? AND conversation_id=? "
            "AND occurred_at>=? AND occurred_at<? AND redacted_at IS NULL "
            "ORDER BY occurred_at LIMIT ?",
            (scope.agent_id, scope.conversation_id, float(start_ts), float(end_ts), int(limit)),
        )
        return [self._event(r) for r in rows]

    def list_capability_gaps(self, scope: Scope, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT id,normalized_need,status,attempts,attempt_budget,updated_at "
            "FROM capability_gaps WHERE agent_id=? AND conversation_id=? "
            "ORDER BY updated_at DESC LIMIT ?",
            (scope.agent_id, scope.conversation_id, int(limit)),
        )
        return [dict(r) for r in rows]

    def list_evolution_proposals(self, scope: Scope, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT id,gap_id,kind,status,expected_gain,updated_at "
            "FROM evolution_proposals WHERE agent_id=? AND conversation_id=? "
            "ORDER BY updated_at DESC LIMIT ?",
            (scope.agent_id, scope.conversation_id, int(limit)),
        )
        return [dict(r) for r in rows]

    def list_study_threads(self, scope: Scope, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT id,topic,specific_question,status,next_step,updated_at "
            "FROM study_threads WHERE agent_id=? AND conversation_id=? "
            "ORDER BY updated_at DESC LIMIT ?",
            (scope.agent_id, scope.conversation_id, int(limit)),
        )
        return [dict(r) for r in rows]

    def redact_events(self, event_ids: list[str]) -> int:
        ids = [str(x) for x in dict.fromkeys(event_ids) if x]
        if not ids:
            return 0
        marks = ",".join("?" for _ in ids)
        with self.tx():
            for event_id in ids:
                self._exec("DELETE FROM meta WHERE k=?", ("input_intent:" + event_id,))
            cur = self._exec(
                f"UPDATE events SET redacted_at=? WHERE id IN ({marks}) AND redacted_at IS NULL",
                [now_ts(), *ids],
            )
            return int(cur.rowcount or 0)

    def cancel_open_loops_for_events(self, event_ids: list[str]) -> int:
        with self.tx():
            return self._cancel_open_loops_for_events(event_ids)

    def _cancel_open_loops_for_events(self, event_ids: list[str]) -> int:
        ids = set(str(x) for x in event_ids)
        if not ids:
            return 0
        cancelled = 0
        rows = self._all(
            "SELECT id,source_event_ids FROM open_loops "
            "WHERE status NOT IN ('succeeded','cancelled','retired')")
        for row in rows:
            sources = set(_loads(row["source_event_ids"], []))
            if sources & ids:
                self._exec(
                    "UPDATE open_loops SET status='cancelled', updated_at=?, version=version+1 "
                    "WHERE id=?", (now_ts(), row["id"]))
                cancelled += 1
        return cancelled

    def prepare_action_once(self, spec) -> str:
        from .actions import spec_hash
        h = spec_hash(spec)
        with self.tx():
            row = self._one(
                "SELECT id FROM action_attempts WHERE action_id=? AND spec_hash=?",
                (spec.id, h))
            if row:
                return str(row["id"])
            attempt_id = new_id("act")
            self._exec(
                "INSERT INTO action_attempts(id,action_id,agent_id,conversation_id,"
                "canonical_spec,spec_hash,status,lease_generation,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,'prepared',1,?,?)",
                (attempt_id, spec.id, spec.scope.agent_id, spec.scope.conversation_id,
                 _dumps({"kind": spec.kind, "target": spec.recipient_or_resource,
                         "effects": spec.requested_effects,
                         "postconditions": spec.postconditions}),
                 h, now_ts(), now_ts()),
            )
            return attempt_id

    def get_action_attempt(self, attempt_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM action_attempts WHERE id=?", (attempt_id,))
        return dict(row) if row else None

    def mark_action_dispatching(self, attempt_id: str) -> bool:
        with self.tx():
            cur = self._exec(
                "UPDATE action_attempts SET status='dispatching', started_at=?, updated_at=? "
                "WHERE id=? AND status='prepared'",
                (now_ts(), now_ts(), attempt_id))
            return cur.rowcount == 1

    def record_action_result(self, attempt_id: str, result) -> bool:
        with self.tx():
            cur = self._exec(
                "UPDATE action_attempts SET status='result_recorded', result=?, updated_at=? "
                "WHERE id=? AND status IN ('dispatching','prepared')",
                (_dumps({
                    "action_id": result.action_id, "status": result.status,
                    "observations": result.observations,
                    "evidence_ids": result.evidence_ids,
                    "postconditions_passed": result.postconditions_passed,
                    "error_category": result.error_category,
                    "actual_cost": result.actual_cost,
                    "next_safe_step": result.next_safe_step,
                }), now_ts(), attempt_id))
            return cur.rowcount == 1

    def action_result(self, attempt_id: str):
        from .actions import ActionResult
        row = self._one("SELECT result FROM action_attempts WHERE id=?", (attempt_id,))
        if not row:
            return None
        data = _loads(row["result"], {})
        return ActionResult(
            action_id=data.get("action_id", ""),
            status=data.get("status", "unknown"),
            observations=data.get("observations", []),
            evidence_ids=data.get("evidence_ids", []),
            postconditions_passed=data.get("postconditions_passed", []),
            error_category=data.get("error_category", ""),
            actual_cost=float(data.get("actual_cost", 0) or 0),
            next_safe_step=data.get("next_safe_step", ""),
        )

    def rescope_legacy(self, agent_id: str, peer: int, target_conversation_id: str) -> dict[str, int]:
        """把某个 peer 的 legacy_unknown 数据显式归到指定会话（用户确认后调用）。"""
        suffix = f":legacy_unknown:{int(peer)}"
        with self.tx():
            self._exec("DELETE FROM meta WHERE k IN (SELECT 'input_intent:' || id FROM events WHERE agent_id=? AND conversation_id=?)",
                       (scope.agent_id, scope.conversation_id))
            e = self._exec(
                "UPDATE events SET conversation_id=? WHERE agent_id=? AND conversation_id LIKE ?",
                (target_conversation_id, agent_id, f"%{suffix}"))
            m = self._exec(
                "UPDATE memories SET conversation_id=? WHERE agent_id=? AND conversation_id LIKE ?",
                (target_conversation_id, agent_id, f"%{suffix}"))
            o = self._exec(
                "UPDATE open_loops SET conversation_id=? WHERE agent_id=? AND conversation_id LIKE ?",
                (target_conversation_id, agent_id, f"%{suffix}"))
            return {"events": int(e.rowcount or 0), "memories": int(m.rowcount or 0),
                    "open_loops": int(o.rowcount or 0)}

    def promote_legacy_memories(self, scope: Scope) -> int:
        with self.tx():
            cur = self._exec(
                "UPDATE memories SET status='active', updated_at=?, "
                "uncertainty_reason=uncertainty_reason || '（已由主人确认）' "
                "WHERE agent_id=? AND conversation_id=? AND source_kind='legacy_unverified' "
                "AND status='candidate'",
                (now_ts(), scope.agent_id, scope.conversation_id),
            )
            return int(cur.rowcount or 0)

    def reset_scope(self, scope: Scope) -> dict[str, int]:
        """重置一段关系：记忆、原话、索引、待办、派生状态一起失效。"""
        sid = scope.scope_id
        with self.tx():
            row = self._one("SELECT epoch FROM forget_epochs WHERE scope_id=?", (sid,))
            epoch = (int(row["epoch"]) if row else 0) + 1
            self._exec("INSERT OR REPLACE INTO forget_epochs(scope_id,epoch) VALUES(?,?)",
                       (sid, epoch))
            m = self._exec(
                "UPDATE memories SET status='redacted', forget_epoch=?, updated_at=? "
                "WHERE agent_id=? AND conversation_id=? AND status<>'redacted'",
                (epoch, now_ts(), scope.agent_id, scope.conversation_id))
            self._exec(
                "DELETE FROM memory_terms WHERE memory_id IN ("
                "SELECT id FROM memories WHERE agent_id=? AND conversation_id=?)",
                (scope.agent_id, scope.conversation_id))
            e = self._exec(
                "UPDATE events SET redacted_at=? WHERE agent_id=? AND conversation_id=? "
                "AND redacted_at IS NULL",
                (now_ts(), scope.agent_id, scope.conversation_id))
            o = self._exec(
                "UPDATE open_loops SET status='cancelled', updated_at=?, version=version+1 "
                "WHERE agent_id=? AND conversation_id=? "
                "AND status NOT IN ('succeeded','cancelled','retired')",
                (now_ts(), scope.agent_id, scope.conversation_id))
            self._exec(
                "DELETE FROM state_snapshots WHERE aggregate_id IN (?,?,?,?)",
                (f"affect:{sid}", f"mood:{sid}", f"relationship:{sid}", f"drive:{sid}"))
            self._exec("DELETE FROM drive_settlements WHERE scope_id=?", (sid,))
            cards = self._exec(
                "DELETE FROM knowledge_cards WHERE agent_id=? AND conversation_id=?",
                (scope.agent_id, scope.conversation_id))
            studies = self._exec(
                "DELETE FROM study_threads WHERE agent_id=? AND conversation_id=?",
                (scope.agent_id, scope.conversation_id))
            gaps = self._exec(
                "DELETE FROM capability_gaps WHERE agent_id=? AND conversation_id=?",
                (scope.agent_id, scope.conversation_id))
            props = self._exec(
                "DELETE FROM evolution_proposals WHERE agent_id=? AND conversation_id=?",
                (scope.agent_id, scope.conversation_id))
            return {"epoch": epoch, "memories": int(m.rowcount or 0),
                    "events": int(e.rowcount or 0), "open_loops": int(o.rowcount or 0),
                    "knowledge_cards": int(cards.rowcount or 0),
                    "studies": int(studies.rowcount or 0),
                    "gaps": int(gaps.rowcount or 0),
                    "proposals": int(props.rowcount or 0)}

    def get_capability_gap(self, gap_id: str):
        from .capability import CapabilityGap
        row = self._one("SELECT * FROM capability_gaps WHERE id=?", (gap_id,))
        if not row:
            return None
        return CapabilityGap(
            id=row["id"],
            scope=Scope(row["agent_id"], row["conversation_id"]),
            normalized_need=row["normalized_need"],
            environment=_loads(row["environment"], {}),
            linked_open_loops=_loads(row["linked_open_loops"], []),
            evidence_ids=_loads(row["evidence_ids"], []),
            trigger_kind=row["trigger_kind"], expected_outcome=row["expected_outcome"],
            acceptance_checks=_loads(row["acceptance_checks"], []),
            status=row["status"], attempt_budget=int(row["attempt_budget"]),
            attempts=int(row["attempts"]), next_review_at=row["next_review_at"],
            failure_signature=row["failure_signature"], version=int(row["version"]),
            created_at=float(row["created_at"]), updated_at=float(row["updated_at"]),
        )

    def get_evolution_proposal(self, proposal_id: str):
        from .capability import EvolutionProposal
        row = self._one("SELECT * FROM evolution_proposals WHERE id=?", (proposal_id,))
        if not row:
            return None
        return EvolutionProposal(
            id=row["id"], gap_id=row["gap_id"],
            scope=Scope(row["agent_id"], row["conversation_id"]),
            kind=row["kind"], hypothesis=row["hypothesis"],
            baseline_evidence_ids=_loads(row["baseline_evidence"], []),
            expected_gain=row["expected_gain"],
            acceptance_checks=_loads(row["acceptance_checks"], []),
            requested_effects=_loads(row["requested_effects"], []),
            experiment_budget=float(row["experiment_budget"]),
            permission_scope_id=row["permission_scope_id"], status=row["status"],
            candidate_artifact=row["candidate_artifact"],
            evaluation_id=row["evaluation_id"], release_id=row["release_id"],
            version=int(row["version"]), created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    def get_study_thread(self, study_id: str):
        from .study import StudyThread
        row = self._one("SELECT * FROM study_threads WHERE id=?", (study_id,))
        if not row:
            return None
        return StudyThread(
            id=row["id"], scope=Scope(row["agent_id"], row["conversation_id"]),
            topic=row["topic"], specific_question=row["specific_question"],
            origin_want_id=row["origin_want_id"],
            why_evidence_ids=_loads(row["why_evidence_ids"], []),
            learning_checks=_loads(row["learning_checks"], []),
            known_card_ids=_loads(row["known_card_ids"], []),
            unresolved_questions=_loads(row["unresolved_questions"], []),
            next_step=row["next_step"], status=row["status"],
            next_review_at=row["next_review_at"], budget=_loads(row["budget"], {}),
            version=int(row["version"]), created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    def update_capability_gap(self, gap_id: str, **fields: Any) -> None:
        allowed = {"status", "attempts", "next_review_at", "version", "updated_at"}
        sets, args = [], []
        for key, value in fields.items():
            if key not in allowed:
                continue
            sets.append(f"{key}=?")
            args.append(value)
        if not sets:
            return
        args.append(gap_id)
        with self.tx():
            self._exec(f"UPDATE capability_gaps SET {', '.join(sets)} WHERE id=?", args)

    def update_evolution_proposal(self, proposal_id: str, **fields: Any) -> None:
        allowed = {"status", "candidate_artifact", "evaluation_id", "release_id",
                   "version", "updated_at"}
        sets, args = [], []
        for key, value in fields.items():
            if key not in allowed:
                continue
            sets.append(f"{key}=?")
            args.append(value)
        if not sets:
            return
        args.append(proposal_id)
        with self.tx():
            self._exec(f"UPDATE evolution_proposals SET {', '.join(sets)} WHERE id=?", args)

    def update_study_thread(self, study_id: str, **fields: Any) -> None:
        allowed = {"next_step", "status", "next_review_at", "known_card_ids",
                   "unresolved_questions", "version", "updated_at"}
        sets, args = [], []
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key in ("known_card_ids", "unresolved_questions"):
                value = _dumps(value)
            sets.append(f"{key}=?")
            args.append(value)
        if not sets:
            return
        args.append(study_id)
        with self.tx():
            self._exec(f"UPDATE study_threads SET {', '.join(sets)} WHERE id=?", args)

    def memory_ids_sharing_events(self, event_ids: list[str]) -> list[str]:
        ids = [str(x) for x in dict.fromkeys(event_ids) if x]
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        rows = self._all(
            f"SELECT DISTINCT memory_id FROM memory_sources WHERE event_id IN ({marks})", ids)
        return [str(r["memory_id"]) for r in rows]

    def redact_memories(self, memory_ids: list[str], epoch: int) -> int:
        ids = [str(x) for x in dict.fromkeys(memory_ids) if x]
        if not ids:
            return 0
        marks = ",".join("?" for _ in ids)
        with self.tx():
            cur = self._exec(
                f"UPDATE memories SET status='redacted', forget_epoch=?, updated_at=? "
                f"WHERE id IN ({marks}) AND status<>'redacted'",
                [int(epoch), now_ts(), *ids])
            self._exec(f"DELETE FROM memory_terms WHERE memory_id IN ({marks})", ids)
            return int(cur.rowcount or 0)

    def redact_knowledge_cards_for_events(self, event_ids: list[str]) -> int:
        ids = [str(x) for x in dict.fromkeys(event_ids) if x]
        if not ids:
            return 0
        marks = ",".join("?" for _ in ids)
        rows = self._all("SELECT id,source_event_ids FROM knowledge_cards")
        hit = []
        for row in rows:
            if set(_loads(row["source_event_ids"], [])) & set(ids):
                hit.append(str(row["id"]))
        if not hit:
            return 0
        hmarks = ",".join("?" for _ in hit)
        with self.tx():
            self._exec(f"DELETE FROM knowledge_cards WHERE id IN ({hmarks})", hit)
        return len(hit)

    def clear_scope_queues(self, scope: Scope) -> dict[str, int]:
        with self.tx():
            cards = self._exec(
                "DELETE FROM knowledge_cards WHERE agent_id=? AND conversation_id=?",
                (scope.agent_id, scope.conversation_id))
            studies = self._exec(
                "DELETE FROM study_threads WHERE agent_id=? AND conversation_id=?",
                (scope.agent_id, scope.conversation_id))
            gaps = self._exec(
                "DELETE FROM capability_gaps WHERE agent_id=? AND conversation_id=?",
                (scope.agent_id, scope.conversation_id))
            props = self._exec(
                "DELETE FROM evolution_proposals WHERE agent_id=? AND conversation_id=?",
                (scope.agent_id, scope.conversation_id))
            return {"knowledge_cards": int(cards.rowcount or 0),
                    "studies": int(studies.rowcount or 0),
                    "gaps": int(gaps.rowcount or 0),
                    "proposals": int(props.rowcount or 0)}

    def find_episode_by_group(self, scope: Scope, event_group_id: str):
        row = self._one(
            "SELECT * FROM memories WHERE agent_id=? AND conversation_id=? "
            "AND kind='episode' AND event_group_id=? AND status<>'redacted' "
            "ORDER BY recorded_at LIMIT 1",
            (scope.agent_id, scope.conversation_id, str(event_group_id)))
        return self._attach_sources([self._memory(row)])[0] if row else None

    def open_loop_exists_for_event(self, scope: Scope, event_id: str) -> bool:
        row = self._one(
            "SELECT 1 FROM open_loops WHERE agent_id=? AND conversation_id=? "
            "AND source_event_ids LIKE ? LIMIT 1",
            (scope.agent_id, scope.conversation_id, f'%"{event_id}"%'))
        return row is not None

    def activate_knowledge_memory(self, scope: Scope, subject: str) -> int:
        with self.tx():
            cur = self._exec(
                "UPDATE memories SET status='active', updated_at=? "
                "WHERE agent_id=? AND conversation_id=? AND kind='knowledge' "
                "AND summary LIKE ? AND status='candidate'",
                (now_ts(), scope.agent_id, scope.conversation_id, f"{subject}：%"))
            return int(cur.rowcount or 0)

    def insert_memory_if_current(self, memory: Memory, terms: list[str],
                                 expected_epoch: int) -> bool:
        """在同一事务里复核 epoch 与来源，再插入记忆；防止删除/重置后复活。"""
        with self.tx():
            if self.forget_epoch(memory.scope.scope_id) != int(expected_epoch):
                return False
            pending = list(memory.derived_from)
            seen: set[str] = set()
            while pending:
                parent_id = pending.pop()
                if parent_id in seen:
                    continue
                seen.add(parent_id)
                parent = self.get_memory(parent_id)
                if (parent is None or parent.status == "redacted"
                        or parent.scope.agent_id != memory.scope.agent_id
                        or parent.scope.conversation_id != memory.scope.conversation_id):
                    return False
                # Validate parent evidence too; it may have been hidden by another deletion.
                for source in parent.sources:
                    event = self.get_event(source.event_id)
                    if (event is None or event.redacted_at is not None
                            or event.scope.private_key != memory.scope.private_key):
                        return False
                pending.extend(parent.derived_from)
            for src in memory.sources:
                row = self._one(
                    "SELECT redacted_at,agent_id,conversation_id FROM events WHERE id=?",
                    (src.event_id,))
                if (row is None or row["redacted_at"] is not None
                        or row["agent_id"] != memory.scope.agent_id
                        or row["conversation_id"] != memory.scope.conversation_id):
                    return False
            self._exec(
                "INSERT INTO memories(id,agent_id,conversation_id,audience_policy_id,kind,"
                "subject_id,predicate,normalized_value,summary,event_group_id,topic_tags,"
                "source_kind,status,valid_from,valid_until,time_precision,recorded_at,updated_at,"
                "version,salience,source_quality,uncertainty_reason,supersedes_id,derived_from,"
                "last_mentioned_at,mention_count_window,retention_policy_id,forget_epoch) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    memory.id, memory.scope.agent_id, memory.scope.conversation_id,
                    memory.scope.audience_policy_id, memory.kind, memory.subject_id,
                    memory.predicate, memory.normalized_value, memory.summary,
                    memory.event_group_id, _dumps(memory.topic_tags), memory.source_kind,
                    memory.status, memory.valid_from, memory.valid_until, memory.time_precision,
                    memory.recorded_at, memory.updated_at, memory.version, memory.salience,
                    memory.source_quality, memory.uncertainty_reason, memory.supersedes_id,
                    _dumps(memory.derived_from), memory.last_mentioned_at,
                    memory.mention_count_window, memory.retention_policy_id,
                    int(expected_epoch),
                ),
            )
            for src in memory.sources:
                self._exec(
                    "INSERT OR IGNORE INTO memory_sources(memory_id,event_id,relation,quote_start,quote_end) "
                    "VALUES(?,?,?,?,?)",
                    (memory.id, src.event_id, src.relation, src.quote_start, src.quote_end),
                )
            self._set_terms(memory.id, terms)
            return True

    def memory_ids_derived_from(self, memory_ids: list[str]) -> list[str]:
        seen = set(str(x) for x in memory_ids)
        frontier = list(seen)
        while frontier:
            rows = self._all("SELECT id,derived_from FROM memories")
            new = []
            for row in rows:
                refs = set(_loads(row["derived_from"], []))
                if refs & set(frontier) and row["id"] not in seen:
                    seen.add(str(row["id"]))
                    new.append(str(row["id"]))
            frontier = new
        return list(seen)

    def activate_knowledge_memory_by_id(self, memory_id: str, scope: Scope) -> int:
        with self.tx():
            cur = self._exec(
                "UPDATE memories SET status='active', updated_at=? "
                "WHERE id=? AND agent_id=? AND conversation_id=? "
                "AND kind='knowledge' AND status='candidate'",
                (now_ts(), memory_id, scope.agent_id, scope.conversation_id))
            return int(cur.rowcount or 0)
