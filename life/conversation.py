"""Canonical message intake and durable conversation work, shared by adapters."""
from __future__ import annotations

from .delivery import DeliveryContext


class ConversationRuntime:
    def __init__(self, runtime):
        self.runtime = runtime
        self.repo = runtime.repo

    def receive(self, scope, text, *, external_id=None, queued=True, images=None):
        with self.repo.tx():
            event, created = self.runtime.ingest(scope, "message", "user_report", text, role="user",
                                                  external_id=external_id, payload={"images": list(images or [])})
            turn = self.runtime.ensure_turn(scope, event.id)
            if not created:
                return event, turn, False, None
            ctx = self.runtime.delivery.new_input(scope, turn.id, [event.id])
            self.runtime.policy.acknowledge_contact(scope)
            if queued:
                self.runtime.jobs.enqueue(scope, "respond", {"turn_id": turn.id, "revision": ctx.revision},
                                          key="respond:" + turn.id, root_id=event.id)
            self.runtime.jobs.enqueue(scope, "project", {"turn_id": turn.id},
                                      key="project:" + turn.id, root_id=event.id)
            return event, turn, True, ctx

    def context(self, lease):
        turn = self.repo.get_turn(lease.payload["turn_id"])
        if turn is None or turn.scope.scope_id != lease.scope_id:
            raise ValueError("turn_not_in_job_scope")
        ctx = DeliveryContext(self.runtime.delivery, turn.scope, turn.id,
                               int(lease.payload["revision"]), tuple(turn.input_event_ids),
                               lease.forget_epoch, lease.policy_epoch)
        ctx.lease = lease
        return ctx

    async def project(self, lease):
        turn = self.repo.get_turn(lease.payload["turn_id"])
        if turn is None:
            self.runtime.jobs.finish(lease, "cancelled")
            return
        with self.runtime.jobs.commit(lease):
            self.runtime.project(turn.scope)
            self.runtime.jobs.finish(lease)
