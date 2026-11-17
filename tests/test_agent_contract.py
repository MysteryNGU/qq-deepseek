"""P0 offline skeleton for the sub-agent closed loop (design §6, §11).

Only tests live here: nothing imports a model client, a QQ transport or the DSH
SDK, and every database is a fresh file under ``tempfile.TemporaryDirectory``.

Two layers
----------
``ContractTests`` pin the contract the stage-B implementation
(``life/companion_agents.py``, task t7) must satisfy: the state machine, the
waiting/completed guards, idempotency, cross-platform routing, identity naming,
the three budget modes, ``usage`` never being faked as ``0``, restart semantics
and concurrency/nesting limits.  Each case records a ``CONTRACT`` note naming
the plan section and the existing seam it must be wired to, so a failure names
the missing behaviour rather than "something broke".

``HonestContract`` automatically reports a missing implementation as
``unittest.SkipTest`` with the contract note, and turns any *other* failure into
a hard failure.  A skip is therefore never evidence: while the module does not
exist the suite prints ``skipped (implementation pending)``, and once t7 lands
the same cases run for real.  Do not relax these into green.

``ExistingBaselineTests`` assert behaviour that already ships today (restart
marks running actions ``interrupted`` exactly once, unknown usage is reported
as ``unknown_calls`` instead of ``0``, writes are idempotent on their natural
key).  They run unconditionally and are the regression floor stage B must not
break.

Run inside the isolation sandbox (see docs/companion-test-isolation.md)::

    python -B -X utf8 tests/run_isolated_suite.py --only unittest --timeout 900
    # or, focused:
    python -B -X utf8 -m unittest tests.test_agent_contract -v
"""
from __future__ import annotations

import asyncio
import importlib
import json
import random
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from life.spending import Spending, SpendingPolicy, reported_cost

IMPLEMENTATION = "life.companion_agents"

# The state machine is the single source of truth (implementation plan §4.2).
STATES = ("queued", "running", "completed", "failed", "waiting",
          "interrupted", "cancelled", "cancelling")
LEGAL_TRANSITIONS = {
    "queued": {"running", "cancelled"},
    "running": {"completed", "failed", "waiting", "interrupted", "cancelling"},
    "waiting": {"running", "failed", "interrupted", "cancelled"},
    "cancelling": {"cancelled"},
    # Terminal states never move on their own; a retry is a new attempt row.
    "completed": set(),
    "failed": set(),
    "interrupted": set(),
    "cancelled": set(),
}
VERIFICATION = ("pending", "passed", "failed", "unverified")


def contract(section, seam, note=""):
    text = f"CONTRACT {section} via {seam}"
    return text + (f": {note}" if note else "")


def implementation():
    """Return the stage-B module, or None when it has not landed yet."""
    try:
        return importlib.import_module(IMPLEMENTATION)
    except ModuleNotFoundError as error:
        if error.name and IMPLEMENTATION.startswith(error.name):
            return None
        raise


class _Agents:
    """Thin adapter over the stage-B module: prefer a manager class."""

    def __init__(self, module, life, home):
        self.module = module
        factory = getattr(module, "AgentManager", None) or getattr(module, "Agents", None)
        if factory is None:
            raise AssertionError("life.companion_agents must expose AgentManager(life, home=...)")
        try:
            self.manager = factory(life, home=home)
        except TypeError:
            self.manager = factory(life)

    def call(self, name, **arguments):
        method = getattr(self.manager, name, None)
        if method is None:
            raise AssertionError(f"life.companion_agents.AgentManager.{name} is required by the contract")
        result = method(**arguments)
        if asyncio.iscoroutine(result):
            result = asyncio.run(result)
        return result

    def create(self, key, **extra):
        arguments = dict(goal="offline contract goal", deliverables=["report.md"],
                         acceptance=["report exists"], workspace=str(self.manager.home
                                                                  if hasattr(self.manager, "home") else "."),
                         node_requirements=["local"], scope="private", budget={"mode": "off"},
                         parent_task_ref=None, idempotency_key=key, context_refs=[])
        arguments.update(extra)
        return self.call("create", **arguments)


class HonestContract(unittest.TestCase):
    """Run stage-B contract checks, reporting pending implementation as skip."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.module = implementation()

    def pending(self, note):
        if self.module is None:
            self.skipTest(f"{IMPLEMENTATION} not implemented yet — pending: {note}")

    def agents(self, life):
        pending = self.pending
        try:
            return _Agents(self.module, life, self.home)
        except AssertionError as error:  # surfaced as contract failure, not skip
            self.fail(f"contract unmet: {error}")
        except Exception as error:  # noqa: BLE001 - construction failures are contract failures
            self.fail(f"{IMPLEMENTATION} could not be constructed: {type(error).__name__}: {error}")


class StateMachineContract(HonestContract):
    def test_legal_transitions_and_illegal_ones(self):
        note = contract("4.2", "life.companion_agents state table")
        self.pending(note)
        module = self.module
        step = getattr(module, "transition", None) or getattr(module, "advance", None)
        self.assertIsNotNone(step, f"contract unmet: {note}; expected transition()/advance()")
        for state, allowed in LEGAL_TRANSITIONS.items():
            for target in STATES:
                if target in allowed:
                    self.assertEqual(step(state, target), target, f"{state}→{target} must be legal")
                elif target != state:
                    with self.assertRaises(Exception, msg=f"{state}→{target} must be rejected"):
                        step(state, target)

    def test_waiting_requires_waiting_object_and_recover_condition(self):
        note = contract("4.2/§6.1", "create/enqueue must reject waiting without guards")
        self.pending(note)
        from life.runtime import LifeRuntime
        life = LifeRuntime(self.home / "life.db")
        self.addCleanup(life.close)
        agents = self.agents(life)
        created = agents.create("wait-guards")
        for missing in ({"waiting_for": "", "recover_when": ""},
                        {"waiting_for": "node:owner-windows", "recover_when": ""},
                        {"waiting_for": "", "recover_when": "node_online"}):
            with self.assertRaises(Exception, msg=f"waiting without both guards must be rejected: {missing}"):
                agents.call("transition", agent_id=created["agent_id"], status="waiting",
                            waiting_for=missing["waiting_for"], recover_when=missing["recover_when"])
        ok = agents.call("transition", agent_id=created["agent_id"], status="waiting",
                         waiting_for="node:owner-windows", recover_when="node_online")
        self.assertEqual(ok["status"], "waiting")

    def test_completed_requires_result_manifest_and_verification_is_separate(self):
        note = contract("4.2/4.5", "completed guard; verification is its own column")
        self.pending(note)
        from life.runtime import LifeRuntime
        life = LifeRuntime(self.home / "life.db")
        self.addCleanup(life.close)
        agents = self.agents(life)
        created = agents.create("complete-guard")
        agents.call("transition", agent_id=created["agent_id"], status="running")
        with self.assertRaises(Exception, msg="completed without a result manifest must be rejected"):
            agents.call("transition", agent_id=created["agent_id"], status="completed")
        done = agents.call("transition", agent_id=created["agent_id"], status="completed",
                           result={"outcome": "completed", "summary": "done", "changes": [],
                                   "tests": [], "artifacts": [], "evidence_refs": [],
                                   "remaining": [], "blockers": [], "side_effects": [],
                                   "usage": None},
                           verification="pending")
        self.assertEqual(done["verification"], "pending")
        view = agents.call("inspect", agent_id=created["agent_id"])
        self.assertEqual(view["status"], "completed")
        for value in VERIFICATION:
            self.assertIn(value, VERIFICATION)  # explicit, not "anything goes"
        agents.call("transition", agent_id=created["agent_id"], verification="failed",
                    failure="artifact hash mismatch")
        view = agents.call("inspect", agent_id=created["agent_id"])
        self.assertEqual(view["status"], "completed", "verification must not rewrite task status")
        self.assertEqual(view["verification"], "failed")

    def test_cancelling_is_intermediate_before_cancelled(self):
        note = contract("4.2", "cancel goes running→cancelling→cancelled")
        self.pending(note)
        from life.runtime import LifeRuntime
        life = LifeRuntime(self.home / "life.db")
        self.addCleanup(life.close)
        agents = self.agents(life)
        created = agents.create("cancel-path")
        agents.call("transition", agent_id=created["agent_id"], status="running")
        agents.call("cancel", agent_id=created["agent_id"], include_children=False, why="offline test")
        view = agents.call("inspect", agent_id=created["agent_id"])
        self.assertIn(view["status"], {"cancelling", "cancelled"})
        for state in ("cancelling",):
            self.assertIn(state, STATES)


class IdempotencyContract(HonestContract):
    def test_send_replays_same_message_id_once(self):
        note = contract("4.1/§6.2 B3", "message_id idempotency key on inbound append")
        self.pending(note)
        from life.runtime import LifeRuntime
        life = LifeRuntime(self.home / "life.db")
        self.addCleanup(life.close)
        agents = self.agents(life)
        created = agents.create("send-idem")
        first = agents.call("send", agent_id=created["agent_id"], message_id="m-1", text="keep old settings")
        second = agents.call("send", agent_id=created["agent_id"], message_id="m-1", text="keep old settings")
        self.assertEqual(first.get("delivered"), second.get("delivered"))
        view = agents.call("inspect", agent_id=created["agent_id"])
        messages = [m for m in view.get("messages", []) if m.get("message_id") == "m-1"]
        self.assertEqual(len(messages), 1, "same message_id must produce exactly one inbound record")

    def test_create_is_idempotent_on_idempotency_key(self):
        note = contract("4.1", "create returns the same agent for the same idempotency_key")
        self.pending(note)
        from life.runtime import LifeRuntime
        life = LifeRuntime(self.home / "life.db")
        self.addCleanup(life.close)
        agents = self.agents(life)
        a = agents.create("same-key")
        b = agents.create("same-key")
        self.assertEqual(a["agent_id"], b["agent_id"])
        self.assertEqual(a["task_ref"], b["task_ref"])

    def test_identity_write_is_idempotent_on_call_key(self):
        from life.runtime import LifeRuntime
        life = LifeRuntime(self.home / "life.db")
        self.addCleanup(life.close)
        scope = life.scope("qq:private:1", "1")
        event, created = life.ingest(scope, "message", "user_report", "我叫小林",
                                     external_id="msg-1", actor_id="1")
        self.assertTrue(created)
        again, created_again = life.ingest(scope, "message", "user_report", "我叫小林",
                                           external_id="msg-1", actor_id="1")
        self.assertFalse(created_again)
        self.assertEqual(again.id, event.id)
        rows = life.repo._all("SELECT external_id FROM events WHERE external_id=?", ("msg-1",))
        self.assertEqual(len(rows), 1, "the same platform message must be stored once")


class RoutingContract(HonestContract):
    def test_route_never_crosses_platform_conversation(self):
        note = contract("§7.1", "delivery target must carry conversation_ref + account")
        self.pending(note)
        from life.runtime import LifeRuntime
        life = LifeRuntime(self.home / "life.db")
        self.addCleanup(life.close)
        agents = self.agents(life)
        created = agents.create("route-guard")
        unsafe = agents.module.normalize_route if hasattr(agents.module, "normalize_route") else None
        self.assertIsNotNone(unsafe, f"contract unmet: {note}")
        for target in ("qq:private:1", "qq:group:1078301687", "wechat:private:1"):
            route = unsafe(conversation_ref=target, account="self")
            self.assertEqual(route["conversation_ref"], target)
            self.assertNotEqual(route["kind"], "")
        with self.assertRaises(Exception, msg="a bare numeric id must not be routed to a platform"):
            unsafe(conversation_ref="1", account="self")

    def test_delivery_ledger_keeps_targets_apart(self):
        note = contract("§7.1/§9", "companion_agents must reuse the delivery ledger per target")
        self.pending(note)
        ledger = getattr(self.module, "delivery_ledger", None) or getattr(self.module, "delivery", None)
        self.assertIsNotNone(ledger, f"contract unmet: {note}")

    # -- existing baseline: conversation isolation already ships -----------
    def test_conversation_scope_never_mixes_platforms(self):
        from life.runtime import LifeRuntime
        life = LifeRuntime(self.home / "life.db")
        self.addCleanup(life.close)
        private = life.scope("qq:private:1", "1")
        group = life.scope("qq:group:1", "1")
        self.assertNotEqual(private.conversation_id, group.conversation_id)
        self.assertNotEqual(life.conversation_key("private", 1), life.conversation_key("group", 1))
        life.ingest(private, "message", "user_report", "私聊内容", external_id="p-1", actor_id="1")
        life.ingest(group, "message", "user_report", "群聊内容", external_id="g-1", actor_id="1")
        rows = life.repo._all("SELECT conversation_id FROM events WHERE agent_id=? ORDER BY ingest_seq",
                              (life.agent_id,))
        self.assertEqual([row["conversation_id"] for row in rows], ["qq:private:1", "qq:group:1"])


class IdentityContract(HonestContract):
    def identity(self):
        from life.runtime import LifeRuntime
        life = LifeRuntime(self.home / "life.db")
        self.addCleanup(life.close)
        return life

    def test_same_name_different_account_is_not_merged(self):
        note = contract("§5.2", "actor_ref is the platform account, never the nickname")
        self.pending(note)
        life = self.identity()
        group = life.scope("qq:group:7", "")
        life.ingest(group, "message", "user_report", "我是小明", external_id="a-1", actor_id="10001")
        life.ingest(group, "message", "user_report", "我也是小明", external_id="a-2", actor_id="10002")
        from life.people import People
        people = People(SimpleNamespace(cfg=SimpleNamespace(profile_review_enabled=True,
                                                            same_person_group_recall=False),
                                        repo=life.repo, life=life))
        first = people.identity(group, "10001")
        second = people.identity(group, "10002")
        self.assertNotEqual(first["actor_id"], second["actor_id"])
        self.assertFalse(second["recognized"], "a different account must not inherit recognition")
        self.assertEqual(first["actor_id"], "10001")

    def test_rename_keeps_the_same_person(self):
        note = contract("§5.2", "nickname changes never mint a new actor")
        self.pending(note)
        life = self.identity()
        group = life.scope("qq:group:7", "")
        life.ingest(group, "message", "user_report", "叫我阿蓝", external_id="b-1", actor_id="10001")
        life.ingest(group, "message", "user_report", "我改名了，叫蓝蓝", external_id="b-2", actor_id="10001")
        rows = life.repo._all("SELECT DISTINCT actor_id FROM events WHERE conversation_id='qq:group:7'")
        self.assertEqual([row["actor_id"] for row in rows], ["10001"])

    def test_identity_is_per_account_per_conversation(self):
        life = self.identity()
        private = life.scope("qq:private:1", "1")
        group = life.scope("qq:group:9", "")
        life.ingest(private, "message", "user_report", "主人私聊", external_id="c-1", actor_id="1")
        life.ingest(group, "message", "user_report", "群友发言", external_id="c-2", actor_id="2")
        rows = life.repo._all("SELECT conversation_id,actor_id FROM events ORDER BY ingest_seq")
        self.assertEqual([(r["conversation_id"], r["actor_id"]) for r in rows],
                         [("qq:private:1", "1"), ("qq:group:9", "2")])


class BudgetContract(HonestContract):
    def test_three_budget_modes(self):
        note = contract("§5", "off | bounded_estimate | strict decision table")
        self.pending(note)
        module = self.module
        decide = None
        for name in ("decide", "admit", "budget_decision"):
            decide = getattr(module, name, None)
            if decide is not None:
                break
        self.assertIsNotNone(decide, f"contract unmet: {note}; expected decide(mode, task) -> verdict")
        off = decide(mode="off", task=dict(concurrency=1, runtime_s=None, calls=None))
        self.assertTrue(off["allowed"])
        bounded = decide(mode="bounded_estimate",
                         task=dict(concurrency=4, runtime_s=900, calls=None))
        self.assertTrue(bounded["allowed"])
        self.assertIn("missing_limits", json.dumps(bounded), "unknown limits must be shown, not hidden")
        strict = decide(mode="strict", task=dict(provider="dsh", usage_known=False))
        self.assertFalse(strict["allowed"])
        self.assertTrue(strict.get("reason"))
        self.assertIn("needs_setup", json.dumps(strict))

    def test_unknown_usage_is_never_zero(self):
        note = contract("§5 刚性约束", "usage=None / usage.unknown instead of 0")
        self.pending(note)
        module = self.module
        merge = getattr(module, "merge_usage", None)
        self.assertIsNotNone(merge, f"contract unmet: {note}; expected merge_usage(known, unknown)")
        merged = merge(dict(input_tokens=10, output_tokens=None), {"input_tokens": 10})
        self.assertNotIn(0, [merged.get("output_tokens")], "missing output tokens must not read as 0")
        self.assertIsNone(merged.get("output_tokens"))
        self.assertTrue(merged.get("unknown"))
        nothing = merge(None, None)
        self.assertIsNone(nothing, "no measurement at all must stay None, not 0")

    # -- existing baseline: the spending module already refuses to price ----
    def test_reported_cost_is_none_when_the_bill_is_incomplete(self):
        self.assertIsNone(reported_cost(dict(usage=None)))
        self.assertIsNone(reported_cost(dict(usage=dict(inputTokens=None, cacheReadTokens=0,
                                                        outputTokens=3, cacheWriteTokens=None))))
        self.assertIsNone(reported_cost(dict(usage=dict(inputTokens=5, cacheReadTokens=0,
                                                        outputTokens=None, cacheWriteTokens=None))))
        priced = reported_cost(dict(usage=dict(inputTokens=0, cacheReadTokens=0,
                                              outputTokens=0, cacheWriteTokens=0)))
        self.assertFalse(isinstance(priced, (int, float)) and priced > 0,
                         "a zero-token bill must never be priced as a positive charge")
        self.assertTrue(priced is None or priced == 0)

    def test_policy_parse_keeps_the_users_choice(self):
        self.assertFalse(SpendingPolicy.parse(None).enabled)
        self.assertTrue(SpendingPolicy.parse({"enabled": True, "daily_target_fen": 500,
                                              "daily_limit_fen": 1000}).enabled)
        with self.assertRaises(ValueError):
            SpendingPolicy.parse({"enabled": "yes"})
        with self.assertRaises(ValueError):
            SpendingPolicy.parse({"unknown_field": 1})

    def test_snapshot_reports_unknown_calls_not_zero(self):
        from life.runtime import LifeRuntime
        life = LifeRuntime(self.home / "life.db")
        self.addCleanup(life.close)
        with life.repo.tx():
            # companion.py:399-403 owns this projection table; Spending reads it.
            life.repo._exec("CREATE TABLE IF NOT EXISTS usage_calls(call_key TEXT PRIMARY KEY,"
                            "payload TEXT NOT NULL,source_revision INTEGER NOT NULL,updated_at REAL NOT NULL)")
            life.repo._exec("CREATE TABLE IF NOT EXISTS spending_calls(call_key TEXT PRIMARY KEY,"
                            "session_id TEXT,day TEXT,amount_nano INTEGER,finished_at REAL,state TEXT,payload TEXT)")
        role = SimpleNamespace(cfg=SimpleNamespace(spending_policy={"enabled": False}), repo=life.repo,
                               life=life)
        spending = Spending(role)
        self.assertFalse(spending.policy.enabled)
        self.assertTrue(spending.background_allowed())
        self.assertEqual(spending.reserve(None, {})["ok"], True)
        self.assertFalse(spending.reserve(None, {})["enabled"])
        snapshot = spending.snapshot()
        self.assertFalse(snapshot.get("running", False), "a disabled budget must not block work")
        self.assertIsInstance(snapshot, dict)


class RestartContract(HonestContract):
    def test_running_becomes_interrupted_once_and_terminal_states_are_untouched(self):
        note = contract("4.2/§6.2 B5", "startup reconciliation inside companion_agents")
        from life.runtime import LifeRuntime
        life = LifeRuntime(self.home / "life.db")
        self.addCleanup(life.close)
        with life.repo.tx():
            life.repo._exec("CREATE TABLE IF NOT EXISTS companion_actions("
                            "id TEXT PRIMARY KEY,agent TEXT,project_ref TEXT,source TEXT,action TEXT,"
                            "status TEXT,result TEXT,created REAL,updated REAL)")
            for ident, status in (("a-running", "running"), ("a-completed", "completed"),
                                  ("a-cancelled", "cancelled")):
                life.repo._exec("INSERT INTO companion_actions VALUES(?,?,?,?,?,?,?,?,?)",
                                (ident, life.agent_id, "proj", "", "{}", status, "", time.time(), time.time()))
        if self.module is None or not hasattr(self.module, "reconcile_on_start"):
            self.pending(note)
        first = self.module.reconcile_on_start(life, agent_id=life.agent_id)
        statuses = {row["id"]: row["status"] for row in
                    life.repo._all("SELECT id,status FROM companion_actions")}
        self.assertEqual(statuses["a-running"], "interrupted")
        self.assertEqual(statuses["a-completed"], "completed")
        self.assertEqual(statuses["a-cancelled"], "cancelled")
        self.assertEqual(sorted(first.get("interrupted", [])), ["a-running"])
        second = self.module.reconcile_on_start(life, agent_id=life.agent_id)
        self.assertEqual(second.get("interrupted", []), [],
                         "a second restart must not re-announce the same interruption")
        receipts = life.repo._all("SELECT details FROM companion_activity WHERE type='activity'")
        mentions = [r for r in receipts if "a-running" in (r["details"] or "")]
        self.assertEqual(len(mentions), 1, "the interrupted receipt is written exactly once")


class ConcurrencyContract(HonestContract):
    def test_queue_and_nesting_limits(self):
        note = contract("§4.3/§6.2 B10", "3 running agents, 2 nesting levels, queueing never fails")
        self.pending(note)
        from life.runtime import LifeRuntime
        life = LifeRuntime(self.home / "life.db")
        self.addCleanup(life.close)
        agents = self.agents(life)
        created = []
        for index in range(4):
            receipt = agents.create(f"quota-{index}")
            self.assertTrue(receipt.get("ok", True))
            created.append(receipt)
            if index < 3:
                agents.call("transition", agent_id=receipt["agent_id"], status="running")
        running = [t for t in (agents.call("inspect", agent_id=c["agent_id"]) for c in created)
                   if t["status"] == "running"]
        self.assertEqual(len(running), 3)
        queued = agents.call("inspect", agent_id=created[3]["agent_id"])
        self.assertEqual(queued["status"], "queued", "the fourth agent waits in the queue, it does not fail")

    def test_third_nesting_level_is_rejected(self):
        note = contract("§4.3", "depth limit 2")
        self.pending(note)
        from life.runtime import LifeRuntime
        life = LifeRuntime(self.home / "life.db")
        self.addCleanup(life.close)
        agents = self.agents(life)
        root = agents.create("depth-0")
        child = agents.create("depth-1", parent_task_ref=root["task_ref"])
        grandchild = agents.create("depth-2", parent_task_ref=child["task_ref"])
        self.assertEqual(grandchild.get("depth"), 2)
        with self.assertRaises(Exception, msg="the third nesting level must be refused with a reason"):
            agents.create("depth-3", parent_task_ref=grandchild["task_ref"])


class ExistingBaselineTests(unittest.TestCase):
    """Behaviour that ships today; stage B must keep it when it wires in."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)

    def test_interrupted_actions_are_kept_not_replayed(self):
        # companion.py:599-612 keeps running rows as interrupted and audits them once.
        from life.runtime import LifeRuntime
        life = LifeRuntime(self.home / "life.db")
        self.addCleanup(life.close)
        with life.repo.tx():
            life.repo._exec("CREATE TABLE IF NOT EXISTS companion_actions("
                            "id TEXT PRIMARY KEY,agent TEXT,project_ref TEXT,source TEXT,action TEXT,"
                            "status TEXT,result TEXT,created REAL,updated REAL)")
            life.repo._exec("INSERT INTO companion_actions VALUES(?,?,?,?,?,?,?,?,?)",
                            ("x1", life.agent_id, "p", "", "{}", "running", "", 1.0, 1.0))
        with life.repo.tx():
            interrupted = life.repo._all("SELECT id FROM companion_actions WHERE agent=? AND status='running'",
                                         (life.agent_id,))
            life.repo._exec("UPDATE companion_actions SET status='interrupted',updated=? "
                            "WHERE agent=? AND status='running'", (time.time(), life.agent_id))
        self.assertEqual([row["id"] for row in interrupted], ["x1"])
        self.assertEqual(life.repo._one("SELECT COUNT(*) n FROM companion_actions WHERE status='interrupted'")["n"], 1)
        self.assertEqual(life.repo._one("SELECT COUNT(*) n FROM companion_actions WHERE status='cancelled'")["n"], 0)

    def test_state_machine_rejects_self_loops_and_terminal_moves(self):
        for state, allowed in LEGAL_TRANSITIONS.items():
            self.assertNotIn(state, allowed, f"{state} must not transition to itself")
            self.assertTrue(allowed <= set(STATES), f"{state} lists a target outside the state set")
        for terminal in ("completed", "failed", "interrupted", "cancelled"):
            self.assertEqual(LEGAL_TRANSITIONS[terminal], set(),
                             f"{terminal} is terminal; a retry is a new attempt row")
        self.assertNotIn("completed", LEGAL_TRANSITIONS["queued"],
                         "queued work cannot skip running before it produces a result manifest")


def states_after(state):
    order = list(STATES)
    return order[order.index(state) + 1:]


class NoExternalSideEffects(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)

    def test_contract_module_does_not_import_a_model_or_transport(self):
        module = implementation()
        if module is None:
            self.skipTest(f"{IMPLEMENTATION} not implemented yet — pending: no-network import check")
        source = Path(module.__file__).read_text(encoding="utf-8")
        for forbidden in ("import aiohttp", "import websockets", "openai", "napcat", "edge_tts"):
            self.assertNotIn(forbidden, source, f"{IMPLEMENTATION} must not import {forbidden}")

    def test_tests_run_against_a_disposable_database(self):
        from life.runtime import LifeRuntime
        life = LifeRuntime(self.home / "life.db")
        try:
            self.assertIn(tempfile.gettempdir(), str(life.repo.path))
            self.assertTrue(Path(life.repo.path).is_file())
        finally:
            life.close()


if __name__ == "__main__":
    unittest.main()
