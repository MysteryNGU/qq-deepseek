"""Durable boundaries tested across independent DB connections and crashes."""
import asyncio
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from life.runtime import LifeRuntime
from life.contracts import Appraisal, DesireEvidence, DesireState
from life.state import reduce_desires
from life.work_queue import WorkQueue, calendar_day


class DurableRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "life.db"
        self.life = LifeRuntime(self.path)
        self.other = LifeRuntime(self.path)
        self.scope = self.life.scope("qq:private:42", "42")
        self.clock = [1800000000.0]
        self.life.jobs = WorkQueue(self.life.repo, lambda: self.clock[0])
        self.other.jobs = WorkQueue(self.other.repo, lambda: self.clock[0])
        self.life.delivery.clock = lambda: self.clock[0]
        self.other.delivery.clock = lambda: self.clock[0]

    def tearDown(self):
        self.life.close()
        self.other.close()
        self.temp.cleanup()

    def context(self, mid="1"):
        event, _ = self.life.ingest_message(self.scope, "request", external_id=mid)
        turn = self.life.ensure_turn(self.scope, event.id)
        return self.life.delivery.new_input(self.scope, turn.id, [event.id])

    def prepare(self, ctx, text="reply", segment=0):
        return self.life.delivery.prepare(ctx, "send_private_msg", {"user_id": 42, "message": text}, segment=segment)

    def test_two_connections_cannot_claim_same_job(self):
        self.life.jobs.enqueue(self.scope, "request", {}, key="job")
        lease = self.life.jobs.claim("job", ttl=5)
        self.assertIsNotNone(lease)
        self.assertIsNone(self.other.jobs.claim("job"))
        self.clock[0] += 6
        successor = self.other.jobs.claim("job")
        self.assertIsNotNone(successor)
        self.assertFalse(self.life.jobs.finish(lease))
        self.assertTrue(self.other.jobs.finish(successor))

    def test_resource_lease_fences_workers(self):
        for key in ("a", "b"):
            self.life.jobs.enqueue(self.scope, "request", {}, key=key)
        a, b = self.life.jobs.claim("a"), self.other.jobs.claim("b")
        self.assertTrue(self.life.jobs.acquire(a, "phone", ttl=5))
        self.assertFalse(self.other.jobs.acquire(b, "phone"))
        self.life.jobs.finish(a)
        self.assertTrue(self.other.jobs.acquire(b, "phone"))

    def test_unknown_job_is_quarantined(self):
        self.life.jobs.enqueue(self.scope, "future-version-kind", {}, key="unknown")
        asyncio.run(self.life.jobs.tick({}))
        self.assertEqual("quarantined", self.life.repo._one("SELECT status FROM work_jobs WHERE id='unknown'")[0])

    def test_budget_clock_rollback_does_not_refill(self):
        self.assertTrue(self.life.jobs.spend("root", "search", 1, 1))
        self.clock[0] -= 86400
        self.assertFalse(self.life.jobs.spend("root", "search", 1, 1))
        self.assertEqual("2027-01-15", calendar_day(1800000000))

    def test_new_input_supersedes_unsent_segment(self):
        ctx = self.context()
        first, second = self.prepare(ctx), self.prepare(ctx, "second", 1)
        token = self.life.delivery.claim(first)
        self.assertTrue(self.life.delivery.settle(first, token, {"retcode": 0, "data": {"message_id": 8}}))
        self.context("2")
        self.assertIsNone(self.life.delivery.claim(second))
        events = self.life.repo.events_after(self.scope, 0)
        self.assertEqual(["reply"], [e.text for e in events if e.kind == "delivered_segment"])

    def test_late_receipt_still_records_real_delivery(self):
        ctx = self.context()
        key = self.prepare(ctx)
        token = self.life.delivery.claim(key)
        self.context("2")
        self.assertTrue(self.life.delivery.settle(key, token, {"retcode": 0, "data": {"message_id": 9}}))
        self.assertEqual(1, len([e for e in self.life.repo.events_after(self.scope, 0) if e.kind == "delivered_segment"]))

    def test_unknown_never_retries_and_never_becomes_history(self):
        ctx = self.context()
        key = self.prepare(ctx)
        token = self.life.delivery.claim(key)
        self.life.delivery.recover()
        self.assertIsNone(self.other.delivery.claim(key))
        self.assertFalse(self.life.delivery.settle(key, token, {"retcode": 0}))
        self.assertFalse(any(e.kind == "delivered_segment" for e in self.life.repo.events_after(self.scope, 0)))

    def test_deleted_epoch_blocks_old_model_output(self):
        ctx = self.context()
        self.life.repo.bump_forget_epoch(self.scope.scope_id)
        key = self.prepare(ctx)
        self.assertIsNone(self.life.delivery.claim(key))

    def test_inflight_deleted_receipt_does_not_resurrect_text(self):
        ctx = self.context()
        key = self.prepare(ctx)
        token = self.life.delivery.claim(key)
        self.life.repo.bump_forget_epoch(self.scope.scope_id)
        self.assertTrue(self.life.delivery.settle(key, token, {"retcode": 0, "data": {"message_id": 9}}))
        self.assertFalse(any(e.kind == "delivered_segment" for e in self.life.repo.events_after(self.scope, 0)))

    def test_recipient_and_logical_key_are_bound(self):
        ctx = self.context()
        with self.assertRaises(PermissionError):
            self.life.delivery.prepare(ctx, "send_private_msg", {"user_id": 43, "message": "leak"})
        key = self.prepare(ctx)
        self.assertEqual(key, self.prepare(ctx))
        with self.assertRaises(ValueError):
            self.prepare(ctx, "mutated")
        token = self.life.delivery.claim(key)
        self.assertIsNone(self.other.delivery.claim(key))
        self.assertFalse(self.other.delivery.settle(key, "wrong", {"retcode": 0, "data": {"message_id": 1}}))
        self.assertIsNotNone(token)

    def test_state_side_effect_failure_rolls_back_entire_transition(self):
        event, _ = self.life.ingest_message(self.scope, "谢谢你", external_id="state")
        app = Appraisal(id="rollback-appraisal", scope=self.scope, source_event_ids=[event.id],
                        proposed_affect_delta={"joy": .2}, certainty=1.)
        original = self.life.update_mood
        def crash(*args, **kwargs):
            raise RuntimeError("crash_after_affect")
        self.life.update_mood = crash
        with self.assertRaises(RuntimeError):
            self.life.appraise(self.scope, event, app)
        self.assertIsNone(self.life.repo.load_state(self.life.state_key("affect", self.scope)))
        self.assertEqual([], self.life.replay_journal(self.scope))
        self.life.update_mood = original
        self.assertIsNotNone(self.life.appraise(self.scope, event, app))
        self.assertEqual("duplicate", self.other.appraise(self.scope, event, app).receipt["status"])
        self.assertEqual(1, len(self.life.replay_journal(self.scope)))

    def test_desire_budget_sums_across_dimensions_and_root_restatements(self):
        state = DesireState(self.scope, updated_at=self.clock[0])
        def evidence(key, dim, root):
            return DesireEvidence(key, self.scope, "meaningful_gain", dim, .2, 1., [root])
        state = reduce_desires(state, [evidence("a", "a", "root-a"), evidence("b", "b", "root-b")], self.clock[0], settle_window=True)
        self.assertAlmostEqual(.02, sum(abs(x) for x in state.learned_offset.values()))
        state = reduce_desires(state, [evidence("c", "c", "root-c")], self.clock[0] + 10, settle_window=True)
        self.assertAlmostEqual(.02, sum(abs(x) for x in state.learned_offset.values()))
        repeated = reduce_desires(state, [evidence("d", "a", "root-a")], self.clock[0] + 86400, settle_window=True)
        self.assertEqual(state.learned_offset, repeated.learned_offset)


if __name__ == "__main__":
    unittest.main()
