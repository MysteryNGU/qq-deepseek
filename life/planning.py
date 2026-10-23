"""Free-form wants become durable plans only through registered effect adapters."""
from __future__ import annotations

from dataclasses import dataclass
import inspect
import json

from .actions import ActionResult, ActionSpec, execute_action
from .contracts import now_ts
from .text import stable_id
from .action_notice import action_task


@dataclass(frozen=True)
class PlanAdapter:
    # resolve is trusted application code, not fields claimed by the model.
    resolve: object
    execute: object


class Planner:
    def __init__(self, runtime, clock=now_ts):
        self.runtime, self.repo, self.clock = runtime, runtime.repo, clock
        self.adapters = {}

    def register(self, name, resolve, execute):
        if name in self.adapters:
            raise ValueError("adapter_already_registered")
        self.adapters[name] = PlanAdapter(resolve, execute)

    def propose(self, scope, goal, checks, steps, sources, *, kind="curiosity", expires=None):
        if not str(goal).strip() or not checks or not steps or len(steps) > 12:
            raise ValueError("unverifiable_or_unbounded_want")
        with self.repo.tx():
            events = self.repo.events_by_ids(sources)
            from .control import control_kind
            if any(control_kind(e.text) or self.repo._one(
                    "SELECT 1 FROM meta WHERE k=?", ("stopped_root:" + scope.scope_id + ":" + e.id,)) for e in events):
                raise ValueError("source_task_stopped")
            if (not events or len(events) != len(set(sources))
                    or any(e.scope.private_key != scope.private_key or e.source_kind not in {"user_report", "tool_observation"} for e in events)):
                raise ValueError("want_requires_independent_scoped_sources")
            identity = stable_id("want", scope.scope_id, goal.strip().casefold())
            if self.repo._one("SELECT 1 FROM wants WHERE id=?", (identity,)):
                return identity
            if not self.runtime.jobs.spend(events[0].id, "want_proposals", 1, 2):
                raise ValueError("root_want_budget_exhausted")
            payload = {"goal": goal[:400], "checks": list(checks), "steps": list(steps),
                       "sources": list(sources), "kind": kind, "epoch": self.repo.forget_epoch(scope.scope_id),
                       "grant_id": "", "checkpoint": 0}
            self.repo._exec("INSERT OR IGNORE INTO wants VALUES(?,?,?,?,?,?)",
                            (identity, scope.scope_id, identity, json.dumps(payload, ensure_ascii=False),
                             "proposed", expires or self.clock() + 7 * 86400))
            from .tasks import TaskService
            TaskService(self.repo).ensure(scope, identity, goal, want_id=identity, status='proposed')
            return identity

    def describe(self, scope, want_id):
        row = self.repo._one("SELECT * FROM wants WHERE id=? AND scope_id=?", (want_id, scope.scope_id))
        if row is None:
            return None
        payload = json.loads(row["payload"])
        resources, effects = [], set()
        for step in payload["steps"]:
            adapter = self.adapters.get(step.get("adapter"))
            if adapter is None:
                raise ValueError("missing_plan_adapter")
            resource, required, postconditions = adapter.resolve(scope, step.get("args") or {})
            resources.append(resource)
            effects.update(required)
            if not postconditions:
                raise ValueError("adapter_has_no_verifiable_postcondition")
        if "device_write" in effects and not set(payload["checks"]) & {"device_goal_verified", "procedure_goal_verified"}:
            raise ValueError("device_plan_requires_goal_verification")
        return {"id": want_id, "goal": payload["goal"], "checks": payload["checks"],
                "steps": payload["steps"], "effects": sorted(effects), "resources": resources,
                "status": row["status"], "expires": row["expires"]}

    def activate(self, scope, want_id, *, grant_id=""):
        with self.repo.tx():
            row = self.repo._one("SELECT * FROM wants WHERE id=? AND scope_id=?", (want_id, scope.scope_id))
            if row is None or row["status"] != "proposed" or row["expires"] <= self.clock():
                return False
            payload = json.loads(row["payload"])
            if payload["epoch"] != self.repo.forget_epoch(scope.scope_id):
                return False
            active = self.repo._one("SELECT COUNT(*) FROM wants WHERE scope_id=? AND status='active'", (scope.scope_id,))[0]
            if active >= 2:
                return False
            description = self.describe(scope, want_id)
            if description["effects"] and not self.runtime.policy.allows(
                    grant_id, scope, want_id, description["effects"], description["resources"]):
                return False
            payload["grant_id"] = grant_id
            self.repo._exec("UPDATE wants SET status='active',payload=? WHERE id=?", (json.dumps(payload), want_id))
            from .tasks import TaskService
            TaskService(self.repo).transition(scope.scope_id,want_id,'active',cause=grant_id)
            self.runtime.jobs.enqueue(scope, "plan", {"want_id": want_id, 'sources':payload['sources']}, key="plan:" + want_id,
                                      root_id=want_id)
            return True

    def cancel(self, scope, want_id):
        with self.repo.tx():
            changed = self.repo._exec("UPDATE wants SET status='cancelled' WHERE id=? AND scope_id=? AND status IN ('proposed','active','paused')", (want_id, scope.scope_id)).rowcount
            self.repo._exec("UPDATE work_jobs SET status='cancelled',lease_token='' WHERE id=? AND scope_id=?", ("plan:" + want_id, scope.scope_id))
            if changed:
                from .tasks import TaskService
                TaskService(self.repo).transition(scope.scope_id,want_id,'cancelled',cause='plan_cancelled')
            return bool(changed)

    def pause(self, scope, want_id):
        with self.repo.tx():
            changed = self.repo._exec(
                "UPDATE wants SET status='paused' WHERE id=? AND scope_id=? AND status='active'",
                (want_id, scope.scope_id)).rowcount
            if changed:
                self.repo._exec("UPDATE work_jobs SET status='paused',lease_token='' WHERE id=? AND scope_id=?",
                                ("plan:" + want_id, scope.scope_id))
                from .tasks import TaskService
                TaskService(self.repo).transition(scope.scope_id,want_id,'paused',cause='plan_paused')
            return bool(changed)

    def resume(self, scope, want_id, *, grant_id=""):
        with self.repo.tx():
            row = self.repo._one("SELECT * FROM wants WHERE id=? AND scope_id=?", (want_id, scope.scope_id))
            if row is None or row["status"] not in {"paused", "active"} or row["expires"] <= self.clock():
                return False
            body = json.loads(row["payload"])
            if body["epoch"] != self.repo.forget_epoch(scope.scope_id):
                return False
            description = self.describe(scope, want_id)
            grant_id = grant_id or body["grant_id"]
            if description["effects"] and not self.runtime.policy.allows(
                    grant_id, scope, want_id, description["effects"], description["resources"]):
                return False
            job = self.repo._one("SELECT status FROM work_jobs WHERE id=?", ("plan:" + want_id,))
            if job is None or job["status"] not in {"paused", "waiting"}:
                return False
            body["grant_id"] = grant_id
            self.repo._exec("UPDATE wants SET status='active',payload=? WHERE id=?", (json.dumps(body), want_id))
            from .tasks import TaskService
            TaskService(self.repo).transition(scope.scope_id,want_id,'active',cause=grant_id,user_resume=True)
            self.repo._exec("UPDATE work_jobs SET status='pending',due=?,policy_epoch=? WHERE id=?",
                            (self.clock(), self.runtime.jobs.policy_epoch(scope.scope_id), "plan:" + want_id))
            return True

    @action_task
    async def run(self, lease):
        row = self.repo._one("SELECT * FROM wants WHERE id=? AND scope_id=?", (lease.payload["want_id"], lease.scope_id))
        if row is None or row["status"] != "active" or row["expires"] <= self.clock():
            self.runtime.jobs.finish(lease, "cancelled")
            return
        body = json.loads(row["payload"])
        events = self.repo.events_by_ids(body["sources"])
        if (len(events) != len(set(body["sources"])) or not events
                or any(e.scope.scope_id != lease.scope_id or e.redacted_at is not None for e in events)):
            self.runtime.jobs.finish(lease, "cancelled")
            return
        scope = events[0].scope
        start = int(body["checkpoint"])
        for index, step in enumerate(body["steps"][start:], start):
            if not self.runtime.jobs.current(lease):
                return
            adapter = self.adapters.get(step.get("adapter"))
            if adapter is None:
                self.runtime.jobs.finish(lease, "quarantined", error="unknown_plan_adapter")
                return
            resource, effects, checks = adapter.resolve(scope, step.get("args") or {})
            if not self.runtime.jobs.acquire(lease, resource.split("|", 1)[0]):
                self.runtime.jobs.finish(lease, "pending", delay=5, checkpoint={"step": index})
                return
            notice = getattr(self.runtime, "before_plan_action", None)
            if notice is not None and not await notice(scope, body["goal"], lease):
                self.runtime.jobs.finish(lease, "waiting", error="action_notice_not_delivered")
                return
            spec = ActionSpec(stable_id("step", row["id"], str(index)), scope, step["adapter"],
                              recipient_or_resource=resource, requested_effects=list(effects),
                              postconditions=list(checks), permission_grant_id=body["grant_id"],
                              plan_id=row["id"], idempotency_key=f"{row['id']}:{index}")
            async def execute(_spec):
                if adapter.resolve(scope, step.get("args") or {}) != (resource, effects, checks):
                    return ActionResult(_spec.id, "failed", error_category="adapter_spec_changed")
                value = adapter.execute(scope, dict(step.get("args") or {}), _spec, lease)
                return await value if inspect.isawaitable(value) else value
            result = await execute_action(spec, execute, self.repo, policy=self.runtime.policy,
                                          lease=lease, jobs=self.runtime.jobs)
            if result.status != "succeeded":
                self.runtime.jobs.finish(lease, "waiting" if result.error_category == "waiting_permission" else "unknown",
                                          checkpoint={"step": index, "status": result.status})
                return
            with self.runtime.jobs.commit(lease):
                body["checkpoint"] = index + 1
                body["verified_checks"] = list(set(body.get("verified_checks", [])) | set(result.postconditions_passed))
                body["result_evidence"] = list(set(body.get("result_evidence", [])) | set(result.evidence_ids))
                self.repo._exec("UPDATE wants SET payload=? WHERE id=? AND status='active'", (json.dumps(body), row["id"]))
        with self.runtime.jobs.commit(lease):
            if not set(body["checks"]).issubset(body.get("verified_checks", [])):
                self.runtime.jobs.finish(lease, "waiting", checkpoint={"stage": "verify_goal"})
                return
            self.repo._exec("UPDATE wants SET status='complete' WHERE id=? AND status='active'", (row["id"],))
            from .tasks import TaskService
            TaskService(self.repo).transition(scope.scope_id,row['id'],'done',cause='plan_verified')
            self.runtime.jobs.finish(lease)
