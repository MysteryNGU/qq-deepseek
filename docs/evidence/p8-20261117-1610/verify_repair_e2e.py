# -*- coding: utf-8 -*-
"""P8 复验：阶段 B 执行入口的端到端路径 + 预算三模式（真配置对象）。

要验的是「F2 真的修好了吗」这条硬证据，所以全程**不直接调库函数**：

    假模型返回一段带 actions=[{kind:'agent', op:'create', ...}] 的决定
      → Companion._run_cognition 校验并 _commit（真实决策入口）
      → companion_actions 里落一行 pending（模型侧动作被受理）
      → Companion._agent_worker_one()（生产里真正跑这件事的后台循环）
      → executor.run(action) → ActionExecutor 的 kind=='agent' 分支
      → AgentManager.dispatch(op=create) → 台账里出现 queued 的一条

中途把 AgentManager.create / dispatch 都包一层记录器，证明**确实经过了台账**，
而不是被别的东西塞了一条记录。

隔离规矩与 t9 一致：copytree 源码副本、临时 HOME、全新库、FakeModel、
QQ_OFFLINE=1、COMPANION_FORBID_EXEC=1、不连任何真实模型/QQ。

用法（云端）::

    .venv-companion/bin/python -B -X utf8 tools/stage-c/verify_repair_e2e.py /tmp/p8verify-XXXX
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from types import SimpleNamespace

ROOT = __import__('pathlib').Path(sys.argv[1]).resolve()
PROD = '/opt/qq-deepseek'
sys.path.insert(0, PROD + '/tools/stage-c')
sys.path.insert(0, PROD)
sys.dont_write_bytecode = True

import verify_awareness_agents as harness  # noqa: E402

harness.ROOT = ROOT
harness.PROD_TREE = harness.Path(PROD)
harness.PROD_HOME = harness.Path('/var/lib/companion/xiaoyu')
# 同一个硬保护：临时根不许落在生产树或 .acceptance 里（copytree 会把自己拷进去）
harness._guard_root()

OUT = []


def record(item, ok, detail=None, limited=False):
    row = dict(item=item, ok=bool(ok), limited=bool(limited), detail=detail)
    OUT.append(row)
    flag = 'PASS' if ok else ('LIMITED' if limited else 'FAIL')
    print('[%s] %s :: %s' % (flag, item, json.dumps(detail, ensure_ascii=False, default=str)[:500]), flush=True)
    return row


class DecisionModel:
    """按剧本返回决定：第一轮带 agent 动作，其余轮只回一句话。"""

    MAX_OUTPUT_TOKENS = 8000

    def __init__(self, decision):
        self.decision = decision
        self.calls = 0
        self.seen_actions_enum = None

    async def complete(self, messages, json_mode=True, purpose='', schema=None):
        self.calls += 1
        # 顺便把「模型能看到哪些动作种类」记下来（F2 的一半：DECISION 里有 agent）
        for message in messages:
            content = message.get('content')
            if isinstance(content, str) and '"kind"' in content and 'enum' in content and 'agent' in content:
                self.seen_actions_enum = True
        payload = self.decision if self.calls == 1 else dict(harness.FAKE_DECISION, reply='好')
        return dict(message=dict(content=json.dumps(payload, ensure_ascii=False)),
                    finish_reason='completed', usage=None, model='fake')

    async def close(self):
        return None


# 模型能回的只有 DECISION 里那几个字段（additionalProperties=False）：
# op / goal / arguments。node 与 idempotency_key 进 arguments蛋壳里，这就是它们真实的传输形式。
_AGENT_ARGS = dict(op='create', goal='列出工作目录里的文件', node='echo',
                   idempotency_key='e2e-f2-1')
AGENT_ACTION = dict(kind='agent', op='create', project='派一件小事',
                    goal='列出工作目录里的文件',
                    arguments=json.dumps(_AGENT_ARGS, ensure_ascii=False))


async def main():
    started = time.time()
    os.environ.update(harness.sandbox_env())
    tree = harness.stage_tree()
    sys.path.insert(0, str(tree))
    os.chdir(tree)
    import life.companion_agents as agents_module
    harness.apply_exception_review(agents_module)

    # ---------------------------------------------------------------- 第 2 项：F2 端到端
    home = harness.stage_home(fresh=True, label='home-e2e')
    model = DecisionModel(dict(harness.FAKE_DECISION, reply='我去派一件小事',
                               projects=[dict(key='派一件小事', goal='派一件小事', status='active')],
                               actions=[dict(AGENT_ACTION)]))
    core = harness.new_core(home, model=model)
    try:
        core.active = True
        calls = {'dispatch': 0, 'create': 0}
        original_dispatch = agents_module.AgentManager.dispatch
        original_create = agents_module.AgentManager.create

        async def spy_dispatch(self, task=None, **kwargs):
            calls['dispatch'] += 1
            return await original_dispatch(self, task, **kwargs)

        def spy_create(self, goal, **kwargs):
            calls['create'] += 1
            return original_create(self, goal, **kwargs)

        agents_module.AgentManager.dispatch = spy_dispatch
        agents_module.AgentManager.create = spy_create

        # (a) 模型看得到 agent 这个动作种类
        record('F2-a 模型侧动作目录里有 kind=agent（DECISION 已扩展）',
               'agent' in json.dumps(core._decision_schema() if hasattr(core, '_decision_schema')
                                     else _schema_hint(), ensure_ascii=False)
               or _enum_has_agent(),
               dict(note='见 companion.py DECISION.actions.kind 枚举'))

        # (b) 真的走决策入口：模型返回决定 → 校验 → _commit 落 pending 动作
        text = '帮我派一件小事：列出工作目录里的文件'
        event, _ = core.life.ingest_message(core.scope, text, role='user', external_id='e2e-in-1')
        core._latest_dialogue = event.id
        await core._run_cognition(event, 'dialogue', None)
        row = core.repo._one('SELECT * FROM companion_actions WHERE source=?', (event.id,))
        record('F2-b 决策入口受理了模型侧 action（不是直接调库）',
               row is not None,
               dict(source=event.id, found=dict(row) if row else None,
                    model_calls=model.calls))
        if row is None:
            raise SystemExit('决策入口没有把 agent 动作落成待办，后面的链路无从验证')
        action = json.loads(row['action'])
        record('F2-c 待办里的动作就是模型给的那个 agent 动作',
               action.get('kind') == 'agent' and action.get('op') == 'create',
               dict(kind=action.get('kind'), op=action.get('op'), goal=action.get('goal'),
                    status=row['status']))

        # (c) 真实的后台循环跑一会儿：看它会不会把这条 agent 活送进台账
        before_ledger = core.repo._one('SELECT COUNT(*) AS n FROM companion_agent_tasks WHERE agent=?',
                                       (core.life.agent_id,))['n']
        loop = asyncio.create_task(core._workers())
        try:
            for _ in range(24):
                await asyncio.sleep(0.25)
                if core.repo._one('SELECT COUNT(*) AS n FROM companion_agent_tasks WHERE agent=?',
                                  (core.life.agent_id,))['n'] > before_ledger:
                    break
        finally:
            loop.cancel()
            try:
                await loop
            except BaseException:  # noqa: BLE001
                pass
        after_rows = core.repo._all('SELECT id,status,task_ref,origin FROM companion_agent_tasks WHERE agent=?',
                                    (core.life.agent_id,))
        action_row = core.repo._one('SELECT status,result FROM companion_actions WHERE id=?', (row['id'],))
        # 同一时刻读两个台账：马上就能看出这条活停在哪一层。
        pick = core.repo._one(
            "SELECT a.* FROM companion_actions a JOIN meta m ON m.k='action_task:'||a.project_ref "
            "WHERE a.agent=? AND a.status='pending' AND (? OR json_extract(m.v,'$.origin')='user')"
            " AND COALESCE(json_extract(a.action,'$.kind'),'')!='agent' ORDER BY a.created LIMIT 1",
            (core.life.agent_id, core.active))
        record('F2-d 模型派的 agent 活真正进台账了吗（companion_agent_tasks）',
               len(after_rows) > 0,
               dict(ledger_before=before_ledger, rows=[dict(r) for r in after_rows][:3],
                    action_status=action_row['status'] if action_row else None,
                    action_kind=(json.loads(row['action']) or {}).get('kind'),
                    worker_would_pick_it=bool(pick),
                    calls=dict(calls),
                    where_it_stays='companion_actions（待办）里的 kind=agent 行，'
                                   '没人把它换成台账里的一件子 agent'))
        # 上一步为什么没成：同一条 agent 活只能进台账一次，而下面 F2-f 会另外直接叫一次。
        # 所以这里只断言「上一步叫不叫台账」不在这里判（避免两个探针互相抢账），
        # 改在 F2-j 里用「一条新的模型动作 + 一个已知基线」重做这个断言。
        record('F2-e 「决策流」这条路上台账入口一次都没被调用',
               calls['dispatch'] == 0 and calls['create'] == 0,
               dict(calls=dict(calls),
                    meaning='这不是幂等：这条路根本没把动作送到台账入口。'
                            '另一边直接叫 executor.run（F2-f）反而能进去。'))

        # (d) 直接验执行器的 agent 分支：executor.run(kind=agent, op=create) → 台账 queued
        #     这是 F2 修的那一段代码本身；上面的 F2-d 验的是「谁把它送进来」。
        direct = await core.executor.run(dict(kind='agent', op='create', node='echo',
                                              goal=json.dumps(dict(stdout='直接调执行器')),
                                              idempotency_key='e2e-direct-1'))
        ledger_after_direct = core.repo._one('SELECT COUNT(*) AS n FROM companion_agent_tasks WHERE agent=?',
                                             (core.life.agent_id,))['n']
        record('F2-f executor.run(kind=agent, op=create) 真的分派到台账并返回 queued',
               direct.get('status') == 'queued' and bool(direct.get('agent_id'))
               and ledger_after_direct > len(after_rows),
               dict(status=direct.get('status'), agent_id=direct.get('agent_id'),
                    task_ref=direct.get('task_ref'), operation_id=direct.get('operation_id'),
                    ledger_after=ledger_after_direct, calls=calls))
        inspect = await core.executor.run(dict(kind='agent', op='inspect', agent_id=direct.get('agent_id')))
        record('F2-g 同一条链路读得回同一件（op=inspect）',
               inspect.get('agent_id') == direct.get('agent_id') and bool(inspect.get('status')),
               dict(agent_id=inspect.get('agent_id'), status=inspect.get('status')))
        record('F2-h 结果轮把回执带回了任务主线（TaskService 有记录）',
               _task_rows(core) != [], dict(tasks=_task_rows(core)[:2]))

        # (e) 干净重做：一条**新的**模型动作，先记下基线，再跑真后台循环，
        #     只看「基线之后台账里多没多这一项活」。这才是 F2-d 的硬证据。
        baseline_ids = {row['id'] for row in core.repo._all(
            'SELECT id FROM companion_agent_tasks WHERE agent=?', (core.life.agent_id,))}
        model.decision = dict(harness.FAKE_DECISION, reply='再派一件',
                              projects=[dict(key='再派一件', goal='再派一件', status='active')],
                              actions=[dict(AGENT_ACTION,
                                            arguments=json.dumps(dict(op='create', goal='列出二号目录',
                                                                      node='echo', idempotency_key='e2e-f2-2'),
                                                            ensure_ascii=False),
                                            project='再派一件')])
        model.calls = 0
        event2, _ = core.life.ingest_message(core.scope, '再帮我派一件', role='user',
                                             external_id='e2e-in-2')
        core._latest_dialogue = event2.id
        await core._run_cognition(event2, 'dialogue', None)
        pending2 = core.repo._one('SELECT * FROM companion_actions WHERE source=?', (event2.id,))
        loop = asyncio.create_task(core._workers())
        try:
            for _ in range(24):
                await asyncio.sleep(0.25)
                now_rows = {row['id'] for row in core.repo._all(
                    'SELECT id FROM companion_agent_tasks WHERE agent=?', (core.life.agent_id,))}
                if now_rows - baseline_ids:
                    break
        finally:
            loop.cancel()
            try:
                await loop
            except BaseException:  # noqa: BLE001
                pass
        now_rows = core.repo._all('SELECT id,status,goal FROM companion_agent_tasks WHERE agent=?',
                                  (core.life.agent_id,))
        fresh = [dict(r) for r in now_rows if r['id'] not in baseline_ids]
        action2 = core.repo._one('SELECT status FROM companion_actions WHERE id=?', (pending2['id'],)) \
            if pending2 else None
        record('F2-j 真后台循环把模型新派的 agent 活送进了台账（基线差集非空）',
               len(fresh) > 0,
               dict(baseline=len(baseline_ids), fresh=fresh,
                    action_status=action2['status'] if action2 else None,
                    note='这一步跑的是真的 Companion._workers()'))

        # (d) 反证：执行器没接台账时必须明说「没发生」，不能假装
        from life.companion_executor import ActionExecutor
        bare = ActionExecutor(harness.load_cfg(home), home)
        reply = await bare.run(dict(kind='agent', op='create', goal='x',
                                    arguments=json.dumps(dict(op='create', goal='x'))))
        record('F2-i 反证：执行器没接台账时明说「没发生」而不是假装成功',
               reply.get('status') == 'failed' and '没有发生' in str(reply.get('output')),
               dict(status=reply.get('status'), code=reply.get('code'), output=reply.get('output')))
    finally:
        harness.close_core(core)
        agents_module.AgentManager.dispatch = original_dispatch
        agents_module.AgentManager.create = original_create

    # ---------------------------------------------------------------- 第 3 项：预算三模式（真配置对象）
    from life.companion import Companion
    from life import companion_agents as agents
    from life import companion_awareness as awareness
    for label, policy, expect in (
        ('off', SimpleNamespace(enabled=False), 'queued'),
        ('bounded_estimate', SimpleNamespace(enabled=True, mode='bounded_estimate'), 'queued'),
        ('strict', SimpleNamespace(enabled=True, mode='strict'), 'rejected'),
        ('enabled 无 mode（=strict，不静默降级）', SimpleNamespace(enabled=True), 'rejected'),
    ):
        budget_home = harness.stage_home(fresh=True, label='home-budget-' + label.split('（')[0])
        core = harness.new_core(budget_home)
        try:
            object.__setattr__(core.spending, 'policy', policy)
            mode_seen = agents.budget_mode(core.spending.policy)
            receipt = await core.agents.dispatch(dict(kind='agent', op='create', node='echo',
                                                      goal=json.dumps(dict(stdout='ok')),
                                                      idempotency_key='budget-e2e'))
            verdict = receipt if expect == 'rejected' else dict(status=receipt.get('status'))
            ok = (receipt.get('status') == 'queued') if expect == 'queued' else (
                receipt.get('status') == 'rejected' and receipt.get('needs_setup') is True
                and bool(receipt.get('reason')))
            record('B6-预算 %s' % label, ok,
                   dict(expected=expect, mode=receipt.get('budget', {}).get('mode') or mode_seen,
                        status=receipt.get('status'), needs_setup=receipt.get('needs_setup'),
                        reason=str(receipt.get('reason'))[:220],
                        usage_known=(receipt.get('budget') or {}).get('usage_known')))
            if expect == 'rejected':
                budget = receipt.get('budget') or {}
                record('B6-预算 %s：未知用量没被写成 0' % label,
                       budget.get('usage_known') is False
                       and budget.get('unknown_written_as_zero') in (False, None),
                       dict(budget=budget))
        finally:
            harness.close_core(core)
    # 未知用量在记账层：两边都没数就是 None
    record('B6-未知用量记账：两边都没数 → None（不是 0）',
           agents.merge_usage(None, None) is None,
           dict(merged=agents.merge_usage(None, None)))
    record('B6-未知用量记账：一边有数 → 缺的字段是 None 且标 unknown',
           (agents.merge_usage(dict(input_tokens=10, output_tokens=5, total_tokens=15, cost=None), None) or {}
            ).get('unknown') is True,
           dict(merged=agents.merge_usage(dict(input_tokens=10, output_tokens=5, total_tokens=15, cost=None),
                                          None)))
    record('B6-预算模式解析：enabled=True 无 mode 当 strict（不静默改用户选择）',
           agents.budget_mode(SimpleNamespace(enabled=True)) == 'strict',
           dict(mode=agents.budget_mode(SimpleNamespace(enabled=True))))
    record('B6-awareness 与 agents 的预算口径一致',
           awareness.spending_mode(SimpleNamespace(enabled=True)) == agents.budget_mode(
               SimpleNamespace(enabled=True)),
           dict(awareness=awareness.spending_mode(SimpleNamespace(enabled=True)),
                agents=agents.budget_mode(SimpleNamespace(enabled=True))))

    summary = dict(total=len(OUT), passed=sum(1 for r in OUT if r['ok']),
                   failed=sum(1 for r in OUT if not r['ok'] and not r['limited']),
                   limited=sum(1 for r in OUT if r['limited']),
                   seconds=round(time.time() - started, 1))
    (ROOT / 'e2e_result.json').write_text(json.dumps(dict(summary=summary, rows=OUT), ensure_ascii=False,
                                                     indent=2, default=str), encoding='utf-8')
    print('\nE2E-SUMMARY ' + json.dumps(summary, ensure_ascii=False), flush=True)
    for row in OUT:
        if not row['ok']:
            print('FAILED: %s :: %s' % (row['item'], json.dumps(row['detail'], ensure_ascii=False)[:300]))


def _task_rows(core):
    try:
        rows = core.repo._all("SELECT k,v FROM meta WHERE k LIKE 'action_task:%'")
    except Exception:  # noqa: BLE001
        return []
    return [json.loads(row['v']) for row in rows]


def _enum_has_agent():
    from life.companion import DECISION
    return 'agent' in DECISION['properties']['actions']['items']['properties']['kind']['enum']


def _schema_hint():
    return ''


if __name__ == '__main__':
    asyncio.run(main())
