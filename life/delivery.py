"""One delivery ledger for both long-lived and short-lived OneBot transports.

The ledger records intent before touching the network. A crash after dispatch
requires reconciliation; it never turns an ambiguous send into a retry.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
import json
from typing import Any

from .contracts import Scope, new_id, now_ts
from .text import stable_id


@dataclass
class DeliveryContext:
    service: Any
    scope: Scope
    turn_id: str
    revision: int
    sources: tuple[str, ...]
    forget_epoch: int
    policy_epoch: int
    next_segment: int = 0
    lease: Any = None


active_delivery: ContextVar[DeliveryContext | None] = ContextVar("active_delivery", default=None)


def execution_context_current():
    ctx = active_delivery.get()
    if ctx is None:
        return True
    runtime = ctx.service.runtime
    return bool(ctx.forget_epoch == runtime.repo.forget_epoch(ctx.scope.scope_id)
                and ctx.policy_epoch == runtime.jobs.policy_epoch(ctx.scope.scope_id)
                and ctx.revision == ctx.service.revision(ctx.scope)
                and (ctx.lease is None or runtime.jobs.current(ctx.lease)))


SEND_ACTIONS = {"send_private_msg", "send_group_msg", "send_msg",
                "upload_private_file", "upload_group_file"}


def recipient(action: str, params: dict) -> str:
    if "group_id" in params:
        return "qq:group:" + str(int(params["group_id"]))
    if "user_id" in params:
        return "qq:private:" + str(int(params["user_id"]))
    raise ValueError("missing_delivery_recipient")


class DeliveryService:
    def __init__(self, runtime, clock=now_ts):
        self.runtime, self.repo, self.clock = runtime, runtime.repo, clock

    def revision(self, scope):
        row = self.repo._one("SELECT revision FROM conversation_revisions WHERE scope_id=?", (scope.scope_id,))
        return int(row["revision"]) if row else 0

    def new_input(self, scope, turn_id, sources):
        with self.repo.tx():
            revision = self.revision(scope) + 1
            self.repo._exec("INSERT OR REPLACE INTO conversation_revisions VALUES(?,?)", (scope.scope_id, revision))
            self.repo._exec("UPDATE deliveries SET status='superseded',updated=? WHERE scope_id=? AND status='pending'",
                            (self.clock(), scope.scope_id))
            return DeliveryContext(self, scope, turn_id, revision, tuple(sources),
                                   self.repo.forget_epoch(scope.scope_id),
                                   self.runtime.jobs.policy_epoch(scope.scope_id))

    def _valid(self, row, *, dispatch=True):
        if dispatch and row["context_revision"] != self.revision_id(row["scope_id"]):
            return False
        if row["forget_epoch"] != self.repo.forget_epoch(row["scope_id"]):
            return False
        if dispatch and row["policy_epoch"] != self.runtime.jobs.policy_epoch(row["scope_id"]):
            return False
        for eid in json.loads(row["sources"]):
            event = self.repo.get_event(eid)
            if event is None or event.redacted_at is not None or event.scope.scope_id != row["scope_id"]:
                return False
        return True

    def revision_id(self, scope_id):
        row = self.repo._one("SELECT revision FROM conversation_revisions WHERE scope_id=?", (scope_id,))
        return int(row["revision"]) if row else 0

    def prepare(self, ctx: DeliveryContext, action: str, params: dict, *, segment=None):
        target = recipient(action, params)
        if target != ctx.scope.conversation_id:
            # Extra recipients require a separate, bound execution grant and
            # context. Model-selected targets cannot inherit the reply route.
            raise PermissionError("delivery_recipient_outside_reply_context")
        index = ctx.next_segment if segment is None else int(segment)
        if segment is None:
            ctx.next_segment += 1
        key = stable_id("delivery", ctx.turn_id, str(index))
        payload = json.dumps({"action": action, "params": params}, ensure_ascii=False, sort_keys=True)
        with self.repo.tx():
            existing = self.repo._one("SELECT * FROM deliveries WHERE logical_key=?", (key,))
            if existing:
                if existing["payload"] != payload or existing["recipient"] != target:
                    raise ValueError("delivery_key_reused_with_different_payload")
                return existing["id"]
            self.repo._exec("INSERT INTO deliveries(id,scope_id,turn_id,logical_key,segment,recipient,payload,"
                            "context_revision,forget_epoch,policy_epoch,sources,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (key, ctx.scope.scope_id, ctx.turn_id, key, index, target, payload,
                             ctx.revision, ctx.forget_epoch, ctx.policy_epoch, json.dumps(ctx.sources),
                             self.clock(), self.clock()))
        return key

    def claim(self, delivery_id):
        with self.repo.tx():
            row = self.repo._one("SELECT * FROM deliveries WHERE id=?", (delivery_id,))
            if not row or row["status"] != "pending":
                return None
            if not self._valid(row):
                self.repo._exec("UPDATE deliveries SET status='superseded' WHERE id=?", (delivery_id,))
                return None
            token = new_id("dispatch")
            self.repo._exec("UPDATE deliveries SET status='dispatching',token=?,updated=? WHERE id=? AND status='pending'",
                            (token, self.clock(), delivery_id))
            return token

    def settle(self, delivery_id, token, response=None):
        with self.repo.tx():
            row = self.repo._one("SELECT * FROM deliveries WHERE id=?", (delivery_id,))
            if not row or row["token"] != token or row["status"] not in {"dispatching", "unknown"}:
                return False
            # Protocol acceptance is not proof of delivery. Only a synchronous
            # success with a platform message ID establishes shared history.
            data = response.get("data") if isinstance(response, dict) else None
            confirmed = (isinstance(response, dict) and response.get("retcode") == 0
                         and response.get("status", "ok") == "ok" and isinstance(data, dict)
                         and data.get("message_id") is not None)
            rejected = isinstance(response, dict) and response.get("retcode") not in (None, 0, 1)
            status = "confirmed" if confirmed else ("failed" if rejected else "unknown")
            # Receipts remain useful after deletion, but their body must not
            # resurrect forgotten content or an obsolete task.
            valid = self._valid(row, dispatch=False)
            receipt = {"message_id": data["message_id"]} if confirmed else {}
            self.repo._exec("UPDATE deliveries SET status=?,receipt=?,updated=? WHERE id=? AND token=?",
                            (status, json.dumps(receipt), self.clock(), delivery_id, token))
            if confirmed and valid:
                body = json.loads(row["payload"])
                text = body["params"].get("message", "")
                if not isinstance(text, str):
                    text = ''.join(str(s.get('data',{}).get('text','')) if s.get('type')=='text'
                                   else '['+s.get('type','媒体')+']' for s in text)
                turn = self.repo.get_turn(row["turn_id"])
                agent,conversation = row['scope_id'].split('|',1)
                scope = turn.scope if turn else Scope(agent,conversation,
                    conversation.rsplit(':',1)[1] if conversation.startswith('qq:private:') else '')
                self.runtime.ingest(scope, "delivered_segment", "self_output", text,
                                    role="assistant", external_id="delivery:" + delivery_id,
                                    source_event_ids=json.loads(row["sources"]),
                                    payload={"confirmed": True, "receipt": receipt,
                                             "delivery_id": delivery_id})
            return confirmed

    def recover(self):
        with self.repo.tx():
            return self.repo._exec("UPDATE deliveries SET status='unknown' WHERE status='dispatching'").rowcount

    async def send(self, ctx, action, params, transport):
        if ctx.lease is not None and not self.runtime.jobs.current(ctx.lease):
            return {"retcode": -1, "status": "failed", "message": "delivery_job_lease_lost"}
        delivery_id = self.prepare(ctx, action, params)
        return await self.send_prepared(delivery_id, transport)

    async def send_prepared(self, delivery_id, transport):
        row = self.repo._one("SELECT status,receipt FROM deliveries WHERE id=?", (delivery_id,))
        if row["status"] == "confirmed":
            return {"retcode": 0, "status": "ok", "data": json.loads(row["receipt"])}
        token = self.claim(delivery_id)
        if token is None:
            return {"retcode": -1, "status": "failed", "message": "delivery_not_dispatchable"}
        try:
            stored = self.repo._one("SELECT payload FROM deliveries WHERE id=?", (delivery_id,))
            body = json.loads(stored["payload"])
            action, params = body["action"], body["params"]
            response = await transport(action, params)
        except BaseException:
            self.settle(delivery_id, token)
            raise
        confirmed = self.settle(delivery_id, token, response)
        if not confirmed and isinstance(response, dict) and response.get("retcode") in (None, 0, 1):
            return {"retcode": -1, "status": "failed", "message": "delivery_unknown_reconcile_required"}
        return response


async def guarded_call(action, params, transport):
    ctx = active_delivery.get()
    if ctx is None or action not in SEND_ACTIONS:
        return await transport(action, params)
    return await ctx.service.send(ctx, action, params, transport)


async def send_authorized_message(runtime, origin, target_uid, text, operation_id, transport, grant_id, plan_id):
    """Explicit tool sends share deliveries, with a bound recipient grant."""
    if origin is None or not execution_context_current():
        return {'retcode': -1, 'message': 'missing_or_stale_execution_context'}
    target = f'qq:private:{int(target_uid)}'
    if not runtime.policy.allows(grant_id, origin.scope, plan_id, ['message'], [target]):
        return {'retcode': -1, 'message': 'missing_recipient_grant'}
    scope = runtime.scope(target, str(target_uid))
    event, _ = runtime.ingest(scope, 'explicit_message', 'inference', text,
        external_id=operation_id, source_event_ids=list(origin.sources),
        payload={'origin_scope': origin.scope.scope_id, 'plan_id': plan_id, 'grant_id': grant_id})
    turn = runtime.ensure_turn(scope, event.id, origin='tool')
    ctx = DeliveryContext(runtime.delivery, scope, turn.id, runtime.delivery.revision(scope),
        (event.id,), runtime.repo.forget_epoch(scope.scope_id), runtime.jobs.policy_epoch(scope.scope_id),
        lease=origin.lease)
    return await runtime.delivery.send(ctx, 'send_private_msg', {'user_id': int(target_uid), 'message': text}, transport)
