# -*- coding: utf-8 -*-
"""P6 修复轮的回归测试：F2 执行入口、F3 预算、F4 dsh 守卫、D2 投递路由与嵌套深度。

全部离线：

* 真 ``Companion`` 对象 + 假模型（不连任何模型、不花钱）；
* 子进程后端用 ``node=echo``（脚本化假服务，按 goal 里的 JSON 表演）；
* 临时 HOME + 全新 ``life.db``；不碰 ``/var/lib/companion``，不发 QQ。

跑法::

    .venv-companion/bin/python -B -X utf8 -m unittest tests.test_agent_repair -v
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FAKE_DECISION = dict(reply='', followups=[], remember=[], projects=[], actions=[], wake_after=0)


class FakeModel:
    """只回一个空决定：不连真实模型，也不产生费用。"""

    MAX_OUTPUT_TOKENS = 8000

    def __init__(self):
        self.calls = 0

    async def complete(self, messages, json_mode=True, purpose='', schema=None):
        self.calls += 1
        return dict(message=dict(content=json.dumps(FAKE_DECISION, ensure_ascii=False)),
                    finish_reason='completed', usage=None, model='fake')

    async def close(self):
        return None


def _cfg(home, **overrides):
    """一份最小的真实配置对象（字段与 life.role_config.Config 一致）。"""
    # 真 Companion 构造时会读的字段都给上默认值；测试要改哪一项就覆盖哪一项。
    base = dict(name='测试小鱼', owner=10001, life_db='data/life.db', life_agent='default',
                api_key='', base_url='https://api.deepseek.com', model='deepseek-flash',
                persona='persona.md', unified_persona=False, public_persona='personas/public.md',
                resource_policy={}, spending_policy={}, memory_index={}, embedding={},
                life_dynamics={}, experience={}, companion_autonomy=False,
                dsh_root='', dsh_node='', dsh_home='', dsh_provider='deepseek-official',
                media_library={}, local_media={}, profile_backfill={}, memory_organize={},
                activity={}, known_people={}, friends=[], allow_groups=[], bot_qq=10001,
                quiet_hours='', handoff_seconds=10, typing_chars_per_second=3,
                paste_threshold=1800, media_max_mb=64, media_local_roots=[],
                action_per_hour=500, action_per_day=0, acknowledge_before_work=True,
                bili_plugin='', local_embedding={}, experience_private={})
    base.update(overrides)
    cfg = SimpleNamespace(**base)
    cfg.service = SimpleNamespace(raw=None)
    return cfg


def _policy(raw):
    """真实 SpendingPolicy + 可选 out-of-band 的 mode（部署侧就是这么挂上去的）。"""
    from life.spending import SpendingPolicy
    fields = {key: value for key, value in raw.items() if key != 'mode'}
    policy = SpendingPolicy.parse(fields)
    if 'mode' in raw:
        policy = SimpleNamespace(**{**policy.__dict__, 'mode': raw['mode']})
    return policy


def _strict_parse(raw):
    """真实 SpendingPolicy.parse 的入口：字段校验与取值范围都在它里面。"""
    from life.spending import SpendingPolicy
    return SpendingPolicy.parse(raw)


def _decision(**extra):
    payload = dict(FAKE_DECISION)
    payload.update(extra)
    return payload


class _Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='repair-')
        self.home = Path(self.temp.name)
        (self.home / 'data').mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.temp.cleanup()

    def core(self, **overrides):
        from life.companion import Companion
        self._model = FakeModel()
        cfg = _cfg(self.home, **overrides)
        core = Companion(cfg, self.home, model=self._model)
        self.addCleanup(core.life.close)
        return core


# ---------------------------------------------------------------- F3 预算

class BudgetModes(unittest.TestCase):
    """预算三模式：在真实 SpendingPolicy 上判，create 按裁决执行或拒绝。"""

    def setUp(self):
        from life.spending import SpendingPolicy
        self.parse = SpendingPolicy.parse

    def policy(self, raw):
        # parse() 只接受 SpendingPolicy 自己的字段：mode 是 OUT-OF-BAND 的预算模式，
        # 由部署侧覆盖在策略对象上（见 BudgetOnRealConfig），不进 enabled/额度字段。
        return self.parse(raw)

    def test_enabled_without_mode_stays_strict_and_rejects_with_reason(self):
        from life import companion_agents as agents
        policy = self.policy(dict(enabled=True, daily_target_fen=500, daily_limit_fen=1000))
        self.assertEqual(agents.budget_mode(policy), 'strict')
        verdict = agents.budget_decision(policy, provider='dsh')
        self.assertFalse(verdict['allowed'])
        self.assertIn('strict', verdict['reason'])
        self.assertTrue(verdict['needs_setup'])

    def test_off_allows_and_does_not_account(self):
        from life import companion_agents as agents
        verdict = agents.budget_decision(self.policy(dict(enabled=False)), provider='dsh')
        self.assertTrue(verdict['allowed'])
        self.assertEqual(verdict['mode'], 'off')
        self.assertEqual(verdict['accounting']['mode'], 'off')

    def test_bounded_estimate_allows_and_reports_enforceable_calls_false(self):
        from life import companion_agents as agents
        verdict = agents.decide(mode='bounded_estimate',
                                task=dict(provider='dsh', concurrency=3, runtime_s=900, calls=None))
        self.assertTrue(verdict['allowed'])
        self.assertIn('calls', verdict['missing_limits'])
        self.assertFalse(verdict['enforceable']['calls'])
        self.assertTrue(verdict['enforceable']['concurrency'])
        self.assertEqual(verdict['accounting']['mode'], 'known_or_unknown_never_zero')
        self.assertFalse(verdict['accounting']['unknown_written_as_zero'])

    def test_policy_parse_accepts_mode_and_rejects_illegal_values(self):
        # H3 之后 mode 是 SpendingPolicy 的正式字段：合法值被接受，非法值必须报错，
        # 不能悄悄回落到 strict。
        policy = _strict_parse(dict(enabled=True, mode='bounded_estimate'))
        self.assertEqual(policy.effective_mode(), 'bounded_estimate')
        with self.assertRaises(ValueError) as caught:
            _strict_parse(dict(enabled=True, mode='whatever'))
        self.assertIn('invalid_spending_policy_mode', str(caught.exception))
        self.assertFalse(_strict_parse(dict(enabled=False)).enabled)

    def test_verdict_always_carries_the_contract_fields(self):
        from life import companion_agents as agents
        for mode in ('off', 'bounded_estimate', 'strict'):
            verdict = agents.decide(mode=mode, task=dict(provider='dsh'))
            for key in agents.BUDGET_KEYS:
                self.assertIn(key, verdict, '%s 缺 %s' % (mode, key))
            self.assertIn('mode', verdict['accounting'])
            self.assertEqual(set(verdict['enforceable']), {'concurrency', 'runtime_s', 'calls'})

    def test_unknown_usage_is_null_not_zero(self):
        from life import companion_agents as agents
        merged = agents.merge_usage(dict(input_tokens=10, output_tokens=None), None)
        self.assertIsNone(merged['output_tokens'])
        self.assertTrue(merged['unknown'])
        self.assertIsNone(agents.merge_usage(None, None))
        full = agents.merge_usage(dict(input_tokens=1, output_tokens=2, total_tokens=3, cost=None),
                                  dict(input_tokens=4, output_tokens=5, total_tokens=9, cost=None))
        self.assertIsNone(full['cost'], '缺 cost 不能写成 0')
        self.assertEqual(full['input_tokens'], 5)


class BudgetOnRealConfig(_Base):
    """在真实 Companion + 真实 SpendingPolicy 上各跑一次 create。"""

    def _with_policy(self, raw):
        from life import companion_agents as agents
        from life.spending import SpendingPolicy
        fields = {key: value for key, value in raw.items() if key != 'mode'}
        policy = SpendingPolicy.parse(fields)
        if 'mode' in raw:
            # 部署侧把模式挂在策略对象上（SpendingPolicy 本身没有这个字段）。
            policy = SimpleNamespace(**{**policy.__dict__, 'mode': raw['mode']})
        core = self.core()
        object.__setattr__(core.spending, 'policy', policy)
        manager = core.agents
        manager.core = core
        goal = json.dumps(dict(stdout='ok'), ensure_ascii=False)
        receipt = manager.create(goal, node='echo', idempotency_key='budget-%s' % (raw.get('mode', 'strict'),),
                                 conversation_ref='qq:private:10001')
        return agents, receipt

    def test_off_create_passes(self):
        _, receipt = self._with_policy(dict(enabled=False))
        self.assertEqual(receipt['status'], 'queued')
        self.assertEqual(receipt['budget']['mode'], 'off')

    def test_bounded_estimate_create_passes(self):
        _, receipt = self._with_policy(dict(enabled=True, mode='bounded_estimate'))
        self.assertEqual(receipt['status'], 'queued')
        self.assertTrue(receipt['budget']['allowed'])
        self.assertEqual(receipt['budget']['accounting']['mode'], 'known_or_unknown_never_zero')
        self.assertFalse(receipt['budget']['enforceable']['calls'])

    def test_enabled_without_mode_is_rejected_with_reason(self):
        _, receipt = self._with_policy(dict(enabled=True))
        self.assertEqual(receipt['status'], 'rejected')
        self.assertEqual(receipt['code'], 'budget_strict_unmetered')
        self.assertTrue(receipt['needs_setup'])
        self.assertIn('strict', receipt['reason'])
        self.assertIsNone(receipt['agent_id'])


# ---------------------------------------------------------------- F4 dsh 守卫

class DshGuard(_Base):
    def test_guard_blocks_only_strict_and_carries_reason(self):
        core = self.core()
        results = {}
        for label, raw in (('off', dict(enabled=False)),
                           ('bounded_estimate', dict(enabled=True, mode='bounded_estimate')),
                           ('strict', dict(enabled=True))):
            object.__setattr__(core.spending, 'policy', _policy(raw))
            verdict = core.dsh_budget_verdict()
            results[label] = (core._dsh_budget_blocked(), verdict)
        self.assertFalse(results['off'][0])
        self.assertFalse(results['bounded_estimate'][0], 'bounded_estimate 下 dsh 必须可执行')
        self.assertTrue(results['strict'][0], 'strict 下 dsh 必须被守卫拦下')
        self.assertIn('strict', results['strict'][1]['reason'])

    def test_strict_refusal_is_visible_not_silently_hidden(self):
        from life import companion_awareness as awareness
        # 用临时 DSH 运行时目录：这条测的是「有运行时 + strict」时能力目录怎么报，
        # 没有路口就只能是「运行时缺失」，测不到计费原因。
        runtime = Path(tempfile.mkdtemp(prefix='dsh-stub-'))
        (runtime / 'python/sdk/src/deepseek_harness').mkdir(parents=True)
        (runtime / 'python/sdk/src/deepseek_harness/__init__.py').write_text('# stub', encoding='utf-8')
        (runtime / 'lib').mkdir(parents=True)
        (runtime / 'lib/bin.js').write_text('// stub', encoding='utf-8')
        self.addCleanup(lambda: __import__('shutil').rmtree(runtime, ignore_errors=True))
        node = Path(sys.executable)
        core = self.core()
        # 执行器链上的 `_dsh_paths` 是 DSH 是否可用的唯一判据；这里把它指向临时运行时。
        core.executor._dsh_paths = lambda: (runtime, node, 'lib/bin.js')
        object.__setattr__(core.spending, 'policy', _policy(dict(enabled=True)))
        state = dict(available_actions=['command', 'work', 'dsh'])
        catalogue = awareness.capabilities(core, state)
        entries = [item for item in catalogue if item['capability_ref'] == 'action.dsh']
        self.assertEqual(len(entries), 1, 'dsh 只能有一条能力条目')
        self.assertEqual(entries[0]['status'], 'needs_setup')
        self.assertIn('strict', entries[0]['reason'])
        self.assertIsNotNone(core.executor._dsh_paths(), '运行时有 dsh 入口却报不可用必须是计费原因')

    def test_reported_cost_none_keeps_unknown_not_zero(self):
        from life.spending import reported_cost
        self.assertIsNone(reported_cost(dict(usage=None)))
        self.assertIsNone(reported_cost(dict(usage=dict(inputTokens=None, cacheReadTokens=0,
                                                        outputTokens=5, cacheWriteTokens=None))))
        self.assertIsNone(reported_cost(dict(usage=dict(inputTokens=1, cacheReadTokens=0,
                                                        outputTokens=5, cacheWriteTokens=7))))


# ---------------------------------------------------------------- F2 执行入口

class ExecutorEntry(_Base):
    def test_executor_dispatches_kind_agent_create_and_inspect(self):
        core = self.core()
        self.assertIs(core.executor._agents_manager(), core.agents,
                      '执行器必须能摸到同一个子 agent 台账')
        receipt = asyncio.run(core.executor.run(
            dict(kind='agent', op='create', node='echo', project='接口验证',
                 goal=json.dumps(dict(stdout='接口通了'), ensure_ascii=False))))
        self.assertEqual(receipt['status'], 'queued')
        for key in ('agent_id', 'task_ref', 'operation_id'):
            self.assertTrue(receipt.get(key), '缺少 %s' % key)
        view = asyncio.run(core.executor.run(
            dict(kind='agent', op='inspect', agent_id=receipt['agent_id'])))
        self.assertEqual(view['status'], 'queued')
        self.assertEqual(view['agent_id'], receipt['agent_id'])

    def test_model_action_kind_agent_reaches_the_ledger(self):
        """模型这一轮真的提交了 kind=agent：提交后台账里能查到，且执行器能读回。"""
        core = self.core()
        source, _ = core.life.ingest(core.scope, 'message', 'user_report', '派个子 agent 看看',
                                     external_id='repair-f2-1', actor_id='10001')
        decision = _decision(
            reply='我去派人',
            actions=[dict(kind='agent', project='接口验证', op='create', node='echo',
                          goal='把接口走通', arguments=json.dumps(dict(
                              node='echo', goal=json.dumps(dict(stdout='通了'), ensure_ascii=False),
                              deliverables=['report.md']), ensure_ascii=False))])
        core._normalize_project_refs(decision)
        core._validate_action_projects(decision)
        core._commit(source, decision)
        rows = core.repo._all("SELECT * FROM companion_actions WHERE agent=? ORDER BY created DESC LIMIT 1",
                              (core.life.agent_id,))
        self.assertEqual(len(rows), 1)
        action = json.loads(rows[0]['action'])
        self.assertEqual(action['kind'], 'agent')
        result = asyncio.run(core.executor.run(action))
        self.assertEqual(result['status'], 'queued', result)
        self.assertTrue(result['agent_id'] and result['task_ref'] and result['operation_id'])

    def test_unwired_executor_reports_not_wired(self):
        from life.companion_executor import ActionExecutor
        cfg = _cfg(self.home)
        executor = ActionExecutor(cfg, self.home)
        result = asyncio.run(executor.run(dict(kind='agent', op='inspect', agent_id='x')))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['code'], 'agent_not_wired')


# ---------------------------------------------------------------- D2 路由与嵌套

class DeliveryRoute(_Base):
    def test_queue_rejects_bare_number_and_normalizes(self):
        from life import companion_delivery as delivery
        core = self.core()
        with self.assertRaises(Exception) as caught:
            delivery.queue(core.repo, mid='m-bare', agent_id=core.life.agent_id, turn_id='t',
                           segment=0, text='x', conversation_ref='555', channels=['qq'])
        self.assertIn('conversation_ref_ambiguous', str(caught.exception))
        row = delivery.queue(core.repo, mid='m-ok', agent_id=core.life.agent_id, turn_id='t',
                             segment=1, text='y', conversation_ref='qq:group:555', channels=['qq'])
        self.assertEqual(row['conversation_ref'], 'qq:group:555')

    def test_empty_and_all_are_not_conversations_but_are_allowed(self):
        from life import companion_delivery as delivery
        core = self.core()
        row = delivery.queue(core.repo, mid='m-all', agent_id=core.life.agent_id, turn_id='t',
                             segment=0, text='z', conversation_ref='', channels=['all'])
        self.assertEqual(row['conversation_ref'], '')


class NestingDepth(_Base):
    def test_third_level_is_refused_even_when_task_ref_is_reused(self):
        from life.companion_agents import AgentError
        core = self.core()
        manager = core.agents
        parent = manager.create('父', node='echo', idempotency_key='n-parent',
                                conversation_ref='qq:private:10001')
        parent_row = manager._row(parent['agent_id'])
        manager.set_status(parent_row['id'], 'running')
        manager.set_status(parent_row['id'], 'waiting',
                           waiting_for='node:cloud', recover_when='node_online')
        child_goal = json.dumps(dict(stdout='c'))
        child = manager.create(child_goal, node='echo', idempotency_key='n-child',
                               parent_task_ref=parent['agent_id'],
                               conversation_ref='qq:private:10001')
        subchild = manager.create(child_goal, node='echo', idempotency_key='n-sub',
                                  parent_task_ref=child['agent_id'],
                                  conversation_ref='qq:private:10001')
        child_row = manager._row(child['agent_id'])
        manager.transition(agent_id=child_row['id'], status='running')
        manager.transition(agent_id=child_row['id'], status='waiting',
                           waiting_for='node:cloud', recover_when='node_online')
        with self.assertRaises(AgentError) as caught:
            manager.create(child_goal, node='echo', idempotency_key='n-grand',
                           parent_task_ref=subchild['agent_id'],
                           conversation_ref='qq:private:10001')
        self.assertEqual(caught.exception.code, 'nesting_limit')
        # 按 id 精确取父：深度是记账的 0/1/2，不是全平
        self.assertEqual([manager._row(row['agent_id'])['depth']
                          for row in (parent, child, subchild)], [0, 1, 2])

    def test_shared_task_ref_is_refused_not_guessed(self):
        """同一项目下的兄弟件共用一个 task_ref：按名字找父不唯一，必须拒绝而不是猜。"""
        from life.companion_agents import AgentError
        core = self.core()
        manager = core.agents
        a = manager.create('a', node='echo', idempotency_key='amb-a', project='同一个项目',
                           conversation_ref='qq:private:10001')
        b = manager.create('b', node='echo', idempotency_key='amb-b', project='同一个项目',
                           conversation_ref='qq:private:10001')
        self.assertNotEqual(a['agent_id'], b['agent_id'])
        self.assertEqual(a['task_ref'], b['task_ref'], '同一项目下 task_ref 本来就会复用')
        # 名字在不止一件上时没有唯一父：拒绝而不是猜（猜浅会多放一层出去）。
        with self.assertRaises(AgentError) as caught:
            manager.create('c', node='echo', idempotency_key='amb-c', parent_task_ref=a['task_ref'],
                           conversation_ref='qq:private:10001')
        self.assertEqual(caught.exception.code, 'parent_ambiguous')
        self.assertIn('agent_id', str(caught.exception))
        # 名字唯一时仍然可以按名字挂：
        only = manager.create('only', node='echo', idempotency_key='amb-only', project='独自一件事',
                              conversation_ref='qq:private:10001')
        under = manager.create('under', node='echo', idempotency_key='amb-under',
                               parent_task_ref=only['task_ref'], conversation_ref='qq:private:10001')
        self.assertEqual(manager._row(under['agent_id'])['depth'], 1)

    def test_depth_follows_the_parent_chain_whatever_the_name_is(self):
        """深度沿 parent_id 链数：父子孙如实 0/1/2，第 4 层 nesting_limit。"""
        from life.companion_agents import AgentError
        core = self.core()
        manager = core.agents
        parent = manager.create('父', node='echo', idempotency_key='chain-parent',
                                conversation_ref='qq:private:10001')
        child = manager.create('子', node='echo', idempotency_key='chain-child',
                               parent_task_ref=parent['agent_id'],
                               conversation_ref='qq:private:10001')
        grand = manager.create('孙', node='echo', idempotency_key='chain-grand',
                               parent_task_ref=child['agent_id'],
                               conversation_ref='qq:private:10001')
        self.assertEqual([manager._row(row['agent_id'])['depth']
                          for row in (parent, child, grand)], [0, 1, 2])
        # 即便台账里的 depth 被写成 0（历史数据/手工改动），链上也数得出来。
        with core.repo.tx():
            core.repo._exec("UPDATE companion_agent_tasks SET depth=0 WHERE id=?", (grand['agent_id'],))
        with self.assertRaises(AgentError) as caught:
            manager.create('曾孙', node='echo', idempotency_key='chain-great',
                           parent_task_ref=grand['agent_id'],
                           conversation_ref='qq:private:10001')
        self.assertEqual(caught.exception.code, 'nesting_limit')


class SendHonesty(_Base):
    """H1：worker 不读 stdin，send 只能报 stored，不能报 delivered。"""

    def test_send_reports_stored_and_says_worker_has_not_read(self):
        core = self.core()
        created = core.agents.create('x', node='echo', idempotency_key='h1-send',
                                     conversation_ref='qq:private:10001')
        receipt = core.agents.send(created['agent_id'], text='再加一条', message_id='h1-m1')
        self.assertEqual(receipt['applied'], 'stored')
        self.assertFalse(receipt['delivered'], 'worker 没读，不能说 delivered')
        self.assertEqual(receipt['applied_reason'], 'worker_does_not_read_stdin')
        self.assertIn('worker 尚未读取', receipt['output'])
        again = core.agents.send(created['agent_id'], text='再加一条', message_id='h1-m1')
        self.assertEqual(again['applied'], 'stored')
        self.assertFalse(again['delivered'])
        self.assertIn('尚未读取', again['output'])


class SpendingModeConfig(unittest.TestCase):
    """H3：mode 进 dataclass，非法值必须报错，不静默回落 strict。"""

    def test_mode_field_round_trips_from_config(self):
        from life.spending import SpendingPolicy
        policy = SpendingPolicy.parse(dict(enabled=True, mode='bounded_estimate'))
        self.assertEqual(policy.effective_mode(), 'bounded_estimate')
        self.assertEqual(SpendingPolicy.parse(dict(enabled=True)).effective_mode(), 'strict')
        self.assertEqual(SpendingPolicy.parse(dict(enabled=False, mode='strict')).effective_mode(), 'off')

    def test_illegal_mode_raises_instead_of_falling_back(self):
        from life.spending import SpendingPolicy
        with self.assertRaises(ValueError) as caught:
            SpendingPolicy.parse(dict(enabled=True, mode='unlimited'))
        self.assertIn('invalid_spending_policy_mode', str(caught.exception))
        from life.companion_agents import budget_mode, AgentError
        broken = SimpleNamespace(enabled=True, mode='unlimited')
        with self.assertRaises(AgentError):
            budget_mode(broken)

    def test_no_legacy_duplicate_mode_reader(self):
        from life import companion_awareness as awareness
        self.assertFalse(hasattr(awareness, '_legacy_spending_mode'),
                         '旧的重复模式判定必须删掉，只留一个真源')


class PendingAgentClaim(_Base):
    """F2 最后一环：模型提交的 pending kind=agent 要能被领走并进台账。"""

    def _submit(self, core, **extra):
        source, _ = core.life.ingest(core.scope, 'message', 'user_report', '派个子 agent',
                                     external_id='t16-pending-1', actor_id='10001')
        decision = _decision(reply='去派', actions=[dict(
            kind='agent', project='接口验证', op='create', node='echo', goal='把接口走通',
            arguments=json.dumps(dict(
                node='echo', goal=json.dumps(dict(stdout='通了'), ensure_ascii=False),
                deliverables=['report.md']), ensure_ascii=False))])
        core._normalize_project_refs(decision)
        core._validate_action_projects(decision)
        core._commit(source, decision)
        return core.repo._one("SELECT * FROM companion_actions WHERE agent=? "
                              "AND json_extract(action,'$.kind')='agent' "
                              "ORDER BY created DESC LIMIT 1", (core.life.agent_id,))

    def test_pending_agent_action_is_claimed_and_lands_queued(self):
        core = self.core()
        core.active = True
        row = self._submit(core)
        self.assertIsNotNone(row, '模型提交的 agent 动作必须落进 companion_actions')
        self.assertEqual(row['status'], 'pending')
        claimed = asyncio.run(core._agent_pending_one())
        self.assertTrue(claimed, 'pending 的 agent 行动必须被领走')
        after = core.repo._one('SELECT status FROM companion_actions WHERE id=?', (row['id'],))
        self.assertIn(after['status'], {'queued', 'pending', 'running', 'succeeded'},
                      '回执会由 _settle 记，这里只要求它真的被执行过')
        rows = core.repo._all("SELECT id,status FROM companion_agent_tasks WHERE agent=?",
                              (core.life.agent_id,))
        self.assertEqual(len(rows), 1, '台账里必须出现一件子 agent')
        self.assertIn(rows[0]['status'], {'queued', 'running', 'completed', 'failed'})

    def test_plain_worker_never_picks_agent_rows(self):
        core = self.core()
        core.active = True
        self._submit(core)
        # 普通领活 SQL 仍然排除 agent：转移只发生在 _agent_pending_one。
        picked = core.repo._one(
            "SELECT a.* FROM companion_actions a JOIN meta m ON m.k='action_task:'||a.project_ref "
            "WHERE a.agent=? AND a.status='pending' "
            "AND COALESCE(json_extract(a.action,'$.kind'),'')!='agent' LIMIT 1", (core.life.agent_id,))
        self.assertIsNone(picked)


class ExecutorKindGuards(_Base):
    def test_finished_agent_is_not_rerun_by_node(self):
        core = self.core()
        manager = core.agents
        created = manager.create('x', node='echo', idempotency_key='done-1',
                                 conversation_ref='qq:private:10001')
        with core.repo.tx():
            core.repo._exec("UPDATE companion_agent_tasks SET status='completed', result=? WHERE id=?",
                            (json.dumps(dict(outcome='completed', summary='ok')), created['agent_id']))
        result = asyncio.run(core.executor.run(dict(kind='agent', agent_id=created['agent_id'], goal='x')))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['code'], 'agent_not_runnable', result)
        self.assertIn('resume', result['output'])

    def test_missing_op_is_refused_with_clear_code(self):
        core = self.core()
        result = asyncio.run(core.executor.run(dict(kind='agent', goal='只有目标没有 op')))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['code'], 'op_required')


if __name__ == '__main__':
    unittest.main()
