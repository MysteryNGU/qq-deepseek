"""Persistent jobs: fenced leases, shared device ownership and root-budget accounting."""
from __future__ import annotations

from contextlib import contextmanager
import asyncio
from dataclasses import dataclass
from datetime import datetime
import json
import math
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .contracts import Scope, new_id, now_ts


def calendar_day(at: float, timezone="Asia/Shanghai") -> str:
    return datetime.fromtimestamp(at, ZoneInfo(timezone)).date().isoformat()


@dataclass(frozen=True)
class Lease:
    id: str
    scope_id: str
    kind: str
    token: str
    generation: int
    forget_epoch: int
    policy_epoch: int
    root_id: str
    payload: dict[str, Any]
    checkpoint: dict[str, Any]


class WorkQueue:
    def __init__(self, repo, clock: Callable[[], float] = now_ts):
        self.repo, self.clock = repo, clock

    def policy_epoch(self, scope_id):
        row = self.repo._one("SELECT epoch FROM policies WHERE scope_id=?", (scope_id,))
        return int(row["epoch"]) if row else 0

    def enqueue(self, scope: Scope, kind: str, payload: dict, *, key: str,
                due: float | None = None, root_id: str = "") -> str:
        now = self.clock()
        with self.repo.tx():
            self.repo._exec(
                "INSERT OR IGNORE INTO work_jobs(id,scope_id,kind,payload,due,root_id,"
                "forget_epoch,policy_epoch,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (key, scope.scope_id, kind, json.dumps(payload, ensure_ascii=False),
                 now if due is None else due, root_id or key,
                 self.repo.forget_epoch(scope.scope_id), self.policy_epoch(scope.scope_id), now, now))
        return key

    def claim(self, job_id: str | None = None, ttl: float = 120) -> Lease | None:
        now = self.clock()
        with self.repo.tx():
            sql = ("SELECT * FROM work_jobs WHERE due<=? AND "
                   "(status='pending' OR (status='running' AND lease_until<?))")
            args: list = [now, now]
            if job_id:
                sql += " AND id=?"
                args.append(job_id)
            row = self.repo._one(sql + " ORDER BY due,created LIMIT 1", args)
            if not row:
                return None
            if row["kind"] not in {"respond", "project"} and self.repo._one(
                    "SELECT 1 FROM meta WHERE k=?",
                    ("stopped_root:" + row["scope_id"] + ":" + row["root_id"],)):
                self.repo._exec("UPDATE work_jobs SET status='cancelled',lease_token='',lease_until=0,error='user_control' WHERE id=?", (row["id"],))
                return None
            if (row["forget_epoch"] != self.repo.forget_epoch(row["scope_id"])
                    or row["policy_epoch"] != self.policy_epoch(row["scope_id"])):
                self.repo._exec("UPDATE work_jobs SET status='cancelled',payload='{}',checkpoint='{}' WHERE id=?", (row["id"],))
                return None
            token = new_id("lease")
            generation = int(row["generation"]) + 1
            self.repo._exec("UPDATE work_jobs SET status='running',lease_token=?,lease_until=?,"
                            "generation=?,attempts=attempts+1,updated=? WHERE id=?",
                            (token, now + ttl, generation, now, row["id"]))
            return Lease(row["id"], row["scope_id"], row["kind"], token, generation,
                         row["forget_epoch"], row["policy_epoch"], row["root_id"],
                         json.loads(row["payload"]), json.loads(row["checkpoint"]))

    def current(self, lease: Lease) -> bool:
        row = self.repo._one("SELECT status,lease_token,lease_until FROM work_jobs WHERE id=?", (lease.id,))
        return bool(row and row["status"] == "running" and row["lease_token"] == lease.token
                    and row["lease_until"] >= self.clock()
                    and self.repo.forget_epoch(lease.scope_id) == lease.forget_epoch
                    and self.policy_epoch(lease.scope_id) == lease.policy_epoch)

    @contextmanager
    def commit(self, lease: Lease):
        with self.repo.tx():
            if not self.current(lease):
                raise RuntimeError("job_lease_or_epoch_lost")
            yield

    def renew(self, lease: Lease, ttl=120) -> bool:
        with self.repo.tx():
            if not self.current(lease):
                return False
            self.repo._exec("UPDATE work_jobs SET lease_until=?,updated=? WHERE id=? AND lease_token=?",
                            (self.clock() + ttl, self.clock(), lease.id, lease.token))
            self.repo._exec("UPDATE resource_leases SET until=? WHERE job_id=? AND token=?",
                            (self.clock() + ttl, lease.id, lease.token))
            return True

    def finish(self, lease: Lease, status="done", *, checkpoint=None, delay=0, error="") -> bool:
        if status not in {"done", "pending", "paused", "waiting", "unknown", "quarantined", "cancelled"}:
            raise ValueError("invalid_job_status")
        with self.repo.tx():
            if not self.current(lease):
                return False
            self.repo._exec("UPDATE work_jobs SET status=?,checkpoint=?,due=?,error=?,lease_until=0,"
                            "lease_token='',updated=? WHERE id=? AND lease_token=?",
                            (status, json.dumps(checkpoint or {}), self.clock() + delay,
                             error[:160], self.clock(), lease.id, lease.token))
            self.repo._exec("DELETE FROM resource_leases WHERE job_id=? AND token=?", (lease.id, lease.token))
            return True

    def acquire(self, lease: Lease, resource: str, ttl=120) -> bool:
        with self.repo.tx():
            if not self.current(lease):
                return False
            row = self.repo._one("SELECT * FROM resource_leases WHERE resource=?", (resource,))
            if row and row["until"] > self.clock() and row["token"] != lease.token:
                return False
            self.repo._exec("INSERT OR REPLACE INTO resource_leases VALUES(?,?,?,?)",
                            (resource, lease.id, lease.token, self.clock() + ttl))
            return True

    def spend(self, root_id: str, category: str, amount: float, limit: float) -> bool:
        if not math.isfinite(amount) or not math.isfinite(limit) or amount < 0 or limit < 0:
            raise ValueError("negative_budget")
        with self.repo.tx():
            # A clock rollback must not create a new spending window.
            last = self.repo._one("SELECT MAX(day) AS day FROM budget_ledger WHERE root_id=? AND category=?",
                                  (root_id, category))
            day = max(calendar_day(self.clock()), str(last["day"] or "") if last else "")
            row = self.repo._one("SELECT used FROM budget_ledger WHERE root_id=? AND day=? AND category=?",
                                 (root_id, day, category))
            used = float(row["used"]) if row else 0.0
            if used + amount > limit:
                return False
            self.repo._exec("INSERT OR REPLACE INTO budget_ledger VALUES(?,?,?,?)",
                            (root_id, day, category, used + amount))
            return True

    async def tick(self, handlers: dict[str, Any], limit=8):
        results = []
        for _ in range(limit):
            lease = self.claim()
            if lease is None:
                break
            handler = handlers.get(lease.kind)
            if handler is None:
                self.finish(lease, "quarantined", error="unknown_job_kind")
                results.append((lease.id, "quarantined"))
                continue
            async def heartbeat():
                while True:
                    await asyncio.sleep(30)
                    if not self.renew(lease):
                        return
            heart = asyncio.create_task(heartbeat())
            try:
                await handler(lease)
                if self.current(lease):
                    self.finish(lease, "pending", delay=60, error="handler_missing_outcome")
            except Exception as exc:
                if self.current(lease):
                    row = self.repo._one("SELECT attempts FROM work_jobs WHERE id=?", (lease.id,))
                    self.finish(lease, "pending", delay=min(3600, 30 * 2 ** min(7, row["attempts"])),
                                error=type(exc).__name__)
            finally:
                heart.cancel()
                await asyncio.gather(heart, return_exceptions=True)
            results.append((lease.id, "handled"))
        return results
