"""Exact plan/effect/resource grants and transactional contact reservations."""
from __future__ import annotations

from datetime import datetime
import json
from zoneinfo import ZoneInfo

from .contracts import new_id, now_ts
from .work_queue import calendar_day
from .authorization import ConsentGate


class PolicyService(ConsentGate):
    def __init__(self, runtime, clock=now_ts):
        self.runtime, self.repo, self.clock = runtime, runtime.repo, clock

    def configure(self, store, cfg, identity):
        self.store, self.cfg, self.identity = store, cfg, identity
        return self

    def grant(self, scope, plan_id, effects, resources, *, expires):
        if not plan_id or expires <= self.clock():
            raise ValueError("invalid_grant")
        identity = new_id("grant")
        with self.repo.tx():
            self.repo._exec("INSERT INTO grants VALUES(?,?,?,?,?,?,?,0)",
                            (identity, scope.scope_id, plan_id, json.dumps(sorted(set(effects))),
                             json.dumps(sorted(set(resources))), self.runtime.jobs.policy_epoch(scope.scope_id), expires))
        return identity

    def allows(self, grant_id, scope, plan_id, effects, resources):
        row = self.repo._one("SELECT * FROM grants WHERE id=?", (grant_id,))
        return bool(row and not row["revoked"] and row["expires"] > self.clock()
                    and row["scope_id"] == scope.scope_id and row["plan_id"] == plan_id
                    and row["epoch"] == self.runtime.jobs.policy_epoch(scope.scope_id)
                    and set(effects).issubset(json.loads(row["effects"]))
                    and set(resources).issubset(json.loads(row["resources"])))

    def set_contact(self, scope, *, enabled=False, quiet_start=22, quiet_end=8, timezone="Asia/Shanghai"):
        ZoneInfo(timezone)
        if not (0 <= quiet_start <= 23 and 0 <= quiet_end <= 23):
            raise ValueError("invalid_quiet_hours")
        with self.repo.tx():
            epoch = self.runtime.jobs.policy_epoch(scope.scope_id) + 1
            self.repo._exec("INSERT OR REPLACE INTO policies VALUES(?,?,?)", (scope.scope_id, epoch,
                json.dumps({"enabled": bool(enabled), "quiet_start": quiet_start,
                            "quiet_end": quiet_end, "timezone": timezone})))

    def reserve_contact(self, scope, root_id, delivery_id):
        """Reserve at most one daily unsolicited contact; unknown stays spent."""
        with self.repo.tx():
            row = self.repo._one("SELECT contact FROM policies WHERE scope_id=?", (scope.scope_id,))
            policy = json.loads(row["contact"]) if row else {}
            if not policy.get("enabled"):
                return False
            hour = datetime.fromtimestamp(self.clock(), ZoneInfo(policy["timezone"])).hour
            start, end = policy["quiet_start"], policy["quiet_end"]
            quiet = (start <= hour < end) if start < end else (hour >= start or hour < end) if start > end else False
            if quiet:
                return False
            day = calendar_day(self.clock(), policy["timezone"])
            rows = self.repo._all("SELECT day,answered FROM share_ledger WHERE scope_id=?", (scope.scope_id,))
            if any(not r["answered"] or r["day"] >= day for r in rows):
                return False
            self.repo._exec("INSERT OR IGNORE INTO share_ledger VALUES(?,?,?,?,0)",
                            (scope.scope_id, root_id, day, delivery_id))
            return bool(self.repo._one("SELECT delivery_id FROM share_ledger WHERE scope_id=? AND root_id=? AND delivery_id=?",
                                       (scope.scope_id, root_id, delivery_id)))

    def acknowledge_contact(self, scope):
        with self.repo.tx():
            self.repo._exec("UPDATE share_ledger SET answered=1 WHERE scope_id=?", (scope.scope_id,))
