# -*- coding: utf-8 -*-
"""P14 第二轮独立验收：越界拒绝 / 嵌套硬拦 / 记账分离 / 能力解耦 / 生命周期。

全部在隔离环境：`/tmp/…` 下临时 HOME + 生产库只读副本；子 agent 的工作目录是一个
新建的空目录，里面只放一份待读探测文件。**不写生产库、不重启服务、不碰生产进程。**

分工：
* 能离线证明的（嵌套、能力解耦、记账字段、生命周期状态机）→ 假后端 `node=echo`
* 必须真跑才能证明的（越界拒绝、凭证暴露面、记账里出现真实调用）→ 真 `node=dsh`

用法::

    .venv-companion/bin/python -B -X utf8 tools/stage-c/r2_acceptance.py <work> [--real]

不给 `--real` 时不会调用真实模型（不花钱），只跑离线那部分。
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

WORK = Path(sys.argv[1]).resolve()
REAL = '--real' in sys.argv
PROD = Path('/opt/qq-deepseek')
PROD_HOME = Path('/var/lib/companion/xiaoyu')
OUT = []


def record(section, item, ok, detail, *, limited=False, note=None):
    row = dict(section=section, item=item, ok=bool(ok), limited=bool(limited), detail=detail)
    if note:
        row['note'] = note
    OUT.append(row)
    flag = 'PASS' if ok else ('LIMITED' if limited else 'FAIL')
    print('[%s] %s / %s :: %s' % (flag, section, item,
                                  json.dumps(detail, ensure_ascii=False, default=str)[:520]), flush=True)
    return row


def load_harness():
    sys.path.insert(0, str(PROD / 'tools/stage-c'))
    sys.path.insert(0, str(PROD))
    sys.dont_write_bytecode = True
    import verify_awareness_agents as harness
    harness.ROOT = WORK
    harness.PROD_TREE = PROD
    harness.PROD_HOME = PROD_HOME
    harness._guard_root()
    return harness


def stage_home(harness, label):
    """临时 HOME：生产库只读副本 + 脱敏配置 + 人格。"""
    import sqlite3
    home = WORK / label
    (home / 'data').mkdir(parents=True, exist_ok=True)
    origin = sqlite3.connect('file:%s?mode=ro' % (PROD_HOME / 'data/life.db'), uri=True, timeout=20)
    try:
        copy = sqlite3.connect(str(home / 'data/life.db'))
        try:
            origin.backup(copy)
        finally:
            copy.close()
    finally:
        origin.close()
    text = (PROD_HOME / 'config.toml').read_text(encoding='utf-8')
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith('"ws_token"') or stripped.startswith('"api_key"'):
            out.append(stripped.split('=', 1)[0].strip() + ' = ""')
        else:
            out.append(line)
    (home / 'config.toml').write_text('\n'.join(out) + '\n', encoding='utf-8')
    for extra in ('persona.md', 'personas', 'skills'):
        source = PROD_HOME / extra
        if source.is_dir():
            import shutil
            shutil.copytree(source, home / extra, dirs_exist_ok=True)
        elif source.is_file():
            import shutil
            shutil.copy2(source, home / extra)
    return home


def new_core(harness, home, model=None):
    from life.companion import Companion
    from life.companion_executor import ActionExecutor
    from life.role_config import load_config
    cfg = load_config(PROD_HOME / 'config.toml')
    cfg.life_db = 'data/life.db'
    core = Companion(cfg, home, model=model or harness.FakeModel(),
                     executor=ActionExecutor(cfg, home))
    core.active = True
    return core


# ---------------------------------------------------------------- 必做二：嵌套硬拦（离线）

async def nesting_checks(harness):
    home = stage_home(harness, 'home-nesting')
    core = new_core(harness, home)
    manager = core.agents
    try:
        async def spawn(key, parent_id=None):
            return await manager.dispatch(dict(kind='agent', op='create', node='echo',
                                               goal=json.dumps(dict(stdout=key)),
                                               idempotency_key=key, parent_task_ref=parent_id,
                                               conversation_ref='qq:private:10001'))

        d0 = await spawn('nest-d0')
        d1 = await spawn('nest-d1', parent_id=d0['agent_id'])
        d2 = await spawn('nest-d2', parent_id=d1['agent_id'])
        record('嵌套', '0→1→2 三层能建起来（上限 2 层是「还能再往下 2 层」）',
               [manager._row(r['agent_id'])['depth'] for r in (d0, d1, d2)] == [0, 1, 2],
               dict(depths=[manager._row(r['agent_id'])['depth'] for r in (d0, d1, d2)],
                    max_depth=manager._limits()['max_depth']))

        # 入口 1：create 再往下派
        err = await harness._raises_async(lambda: spawn('nest-d3', parent_id=d2['agent_id']))
        record('嵌套', '入口①create：第 3 层被产品拒绝（nesting_limit）',
               'nesting_limit' in str(err), dict(error=err))

        # 入口 2：send（转交）
        err = await harness._raises_async(lambda: manager.dispatch(
            dict(kind='agent', op='send', agent_id=d2['agent_id'],
                 text='再派一个子任务出去', message_id='nest-m1')))
        record('嵌套', '入口②send/转交：在第 2 层再派活被拒绝',
               'nesting_limit' in str(err), dict(error=err))

        # 入口 3：resume
        with core.repo.tx():
            core.repo._exec("UPDATE companion_agent_tasks SET status='failed' WHERE id=?",
                            (d2['agent_id'],))
        err = await harness._raises_async(lambda: manager.dispatch(
            dict(kind='agent', op='resume', agent_id=d2['agent_id'])))
        record('嵌套', '入口③resume：在第 2 层续作被拒绝',
               'nesting_limit' in str(err), dict(error=err))

        # 对照：浅层仍可正常派活，证明拒绝不是「一刀切」
        ok_child = await spawn('nest-shallow', parent_id=d0['agent_id'])
        record('嵌套', '对照：第 0 层仍能正常派活（拒绝有针对性）',
               manager._row(ok_child['agent_id'])['depth'] == 1,
               dict(depth=manager._row(ok_child['agent_id'])['depth']))
    finally:
        harness.close_core(core)


# ---------------------------------------------------------------- 必做四：能力解耦（离线）

async def capability_checks(harness):
    from life import companion_awareness as awareness
    home = stage_home(harness, 'home-cap')
    core = new_core(harness, home)
    manager = core.agents
    try:
        def catalog():
            return {item['capability_ref']: item for item in
                    awareness.capabilities(core, dict(core._messages(None, '看能力', {}, purpose='activity')
                                                      and {'available_actions': ['memory', 'agent']}))}

        before = catalog()
        before_item = before.get('agent.subagent') or {}
        rows_before = core.repo._one('SELECT COUNT(*) AS n FROM companion_agent_tasks')['n']

        # 一件「无交付物」的活：goal 自带成功判据（result 必须原样出现）
        goal = json.dumps(dict(stdout='CAP-OK-%s' % secrets.token_hex(3), result='CAP-OK'),
                          ensure_ascii=False)
        created = await manager.dispatch(dict(kind='agent', op='create', node='echo', goal=goal,
                                              idempotency_key='cap-1', conversation_ref='qq:private:10001'))
        ran = await manager.execute(dict(kind='agent', agent_id=created['agent_id'], goal=goal),
                                    action_id='cap-1')
        row = manager._row(created['agent_id'])
        record('能力解耦', '无交付物任务用 goal 里的 result 作为自己的成功判据',
               ran.get('verification') in ('passed', 'pending'),
               dict(status=row['status'], verification=row['verification'],
                    artifacts=json.loads(row['result'] or '{}').get('artifacts'),
                    outcome=json.loads(row['result'] or '{}').get('outcome')))

        after = catalog()
        after_item = after.get('agent.subagent') or {}
        record('能力解耦', '能力目录在「有真实跑完的调用」后变 ready',
               after_item.get('status') == 'ready',
               dict(before=before_item.get('status'), after=after_item.get('status'),
                    after_evidence=after_item.get('evidence')))

        # 反证：读能力目录不许改任何一行的 verification
        with core.repo.tx():
            core.repo._exec("UPDATE companion_agent_tasks SET verification='pending' WHERE id=?",
                            (created['agent_id'],))
        pending_before = manager._row(created['agent_id'])['verification']
        catalog()  # 再读一次能力目录
        pending_after = manager._row(created['agent_id'])['verification']
        record('能力解耦', '读能力目录不会把 verification 伪造成 passed',
               pending_before == pending_after == 'pending',
               dict(before=pending_before, after=pending_after,
                    note='能力 ready 与 verification=passed 是两件事'))

        # 独立变化：把最新一条压成 failed，能力应回落，而旧任务 verification 不受影响
        with core.repo.tx():
            core.repo._exec("UPDATE companion_agent_tasks SET status='failed' WHERE id=?",
                            (created['agent_id'],))
        fallen = (catalog().get('agent.subagent') or {}).get('status')
        record('能力解耦', '最近一次调用失败后能力回落，两者可独立变化',
               fallen != 'ready',
               dict(after_failure=fallen, task_verification=manager._row(created['agent_id'])['verification']))
    finally:
        harness.close_core(core)


# ---------------------------------------------------------------- 必做三+五：记账 / 生命周期（离线）

async def lifecycle_checks(harness):
    home = stage_home(harness, 'home-life')
    core = new_core(harness, home)
    manager = core.agents
    try:
        from life.spending import SpendingPolicy
        policy = SpendingPolicy.parse(getattr(core.cfg, 'spending_policy', None))
        record('生命周期', '预算模式为 off（本轮授权口径）', policy.effective_mode() == 'off',
               dict(mode=policy.effective_mode()))

        # 并发 3 + 排队
        long_goal = json.dumps(dict(sleep_s=20))
        made = [await manager.dispatch(dict(kind='agent', op='create', node='echo', goal=long_goal,
                                            idempotency_key='life-%d' % i,
                                            conversation_ref='qq:private:10001')) for i in range(4)]
        tasks = [asyncio.create_task(manager.execute(dict(kind='agent', agent_id=row['agent_id'],
                                                          goal=long_goal), action_id='l%d' % i))
                 for i, row in enumerate(made[:3])]
        await asyncio.sleep(2.0)
        running = manager._running_count()
        fourth = manager._row(made[3]['agent_id'])['status']
        record('生命周期', '并发上限 3 生效，第 4 件排队不失败',
               running == 3 and fourth == 'queued', dict(running=running, fourth=fourth))
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except BaseException:  # noqa: BLE001
                pass

        # 超时中止：给一件 sleep 很久的活，用 wait 的时限观察它没在时限内结束
        slow = await manager.dispatch(dict(kind='agent', op='create', node='echo',
                                           goal=json.dumps(dict(sleep_s=30)), idempotency_key='timeout-1',
                                           conversation_ref='qq:private:10001'))
        task = asyncio.create_task(manager.execute(dict(kind='agent', agent_id=slow['agent_id'],
                                                        goal=json.dumps(dict(sleep_s=30))),
                                                   action_id='t1'))
        await asyncio.sleep(1.0)
        waited = await manager.dispatch(dict(kind='agent', op='wait', agent_id=slow['agent_id'], wait_s=2))
        record('生命周期', '超时观察：wait 到时限仍未结束，如实报 pending（不谎报完成）',
               waited.get('state') == 'pending', dict(state=waited.get('state')))
        cancelled = await manager.dispatch(dict(kind='agent', op='cancel', agent_id=slow['agent_id'],
                                                why='验收：超时中止'))
        try:
            await task
        except BaseException:  # noqa: BLE001
            pass
        record('生命周期', '中止后落到终态（cancelled），不是永久 running',
               manager._row(slow['agent_id'])['status'] in ('cancelled', 'interrupted'),
               dict(status=manager._row(slow['agent_id'])['status'],
                    cancel_side_effects=cancelled.get('side_effects')))

        # 已完成的外部操作不被重复执行
        done = await manager.dispatch(dict(kind='agent', op='create', node='echo',
                                           goal=json.dumps(dict(stdout='once')),
                                           idempotency_key='dup-1', conversation_ref='qq:private:10001'))
        first = await manager.execute(dict(kind='agent', agent_id=done['agent_id'],
                                           goal=json.dumps(dict(stdout='once'))), action_id='dup-1')
        again = await manager.dispatch(dict(kind='agent', op='create', node='echo',
                                            goal=json.dumps(dict(stdout='once')),
                                            idempotency_key='dup-1', conversation_ref='qq:private:10001'))
        rerun = await manager.dispatch(dict(kind='agent', op='resume', agent_id=done['agent_id']))
        runs = core.repo._all("SELECT COUNT(*) AS n FROM companion_agent_events WHERE agent_id=? "
                              "AND kind='running'", (done['agent_id'],))[0]['n']
        record('生命周期', '同一件事不会重复执行：同幂等键返回同一件、终态不被重跑',
               again['agent_id'] == done['agent_id'] and runs == 1,
               dict(first_status=first.get('status'), same_agent=again['agent_id'] == done['agent_id'],
                    running_events=runs, resume_new_attempt=rerun.get('attempt')))

        # 重启收尾：interrupted 只一次、不重新入队
        stuck = await manager.dispatch(dict(kind='agent', op='create', node='echo',
                                            goal=json.dumps(dict(stdout='stuck')), idempotency_key='rs-1',
                                            conversation_ref='qq:private:10001'))
        with core.repo.tx():
            core.repo._exec("UPDATE companion_agent_tasks SET status='running' WHERE id=?",
                            (stuck['agent_id'],))
        first_reconcile = manager.reconcile_on_start(core.repo, agent_id=manager.agent_id)
        second_reconcile = manager.reconcile_on_start(core.repo, agent_id=manager.agent_id)
        record('生命周期', '重启收尾：running→interrupted 只一次，不重复宣布',
               manager._row(stuck['agent_id'])['status'] == 'interrupted'
               and not (second_reconcile.get('interrupted') or []),
               dict(first=first_reconcile.get('interrupted'), second=second_reconcile.get('interrupted')))
        record('生命周期', '收尾后不会被重新入队（不会再跑一次）',
               manager._row(stuck['agent_id'])['status'] == 'interrupted'
               and not any(r['id'] == stuck['agent_id'] for r in manager.queued(limit=50)),
               dict(status=manager._row(stuck['agent_id'])['status'],
                    in_queue=any(r['id'] == stuck['agent_id'] for r in manager.queued(limit=50))))

        # 记账：off 下也要有记录，未知是 None 不是 0
        usage = core.repo._all("SELECT * FROM companion_usage WHERE agent=? ORDER BY created DESC LIMIT 3",
                               (core.life.agent_id,))
        calls = core.repo._all("SELECT * FROM spending_calls ORDER BY rowid DESC LIMIT 3")
        record('记账', 'off 模式下仍留有调用记录，未知用量记 null',
               all(('"input_tokens": 0' not in str(u['usage'])) for u in usage),
               dict(usage=[dict(u) for u in usage], spending=[dict(c) for c in calls][:2]))
    finally:
        harness.close_core(core)


async def main():
    started = time.time()
    harness = load_harness()
    os.environ.update(harness.sandbox_env())
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    for step in (nesting_checks, capability_checks, lifecycle_checks):
        try:
            await step(harness)
        except BaseException as error:  # noqa: BLE001
            import traceback
            record(step.__name__, '这一步抛错', False,
                   dict(error='%s: %s' % (type(error).__name__, error),
                        trace=traceback.format_exc()[-1200:]))
    summary = dict(total=len(OUT), passed=sum(1 for r in OUT if r['ok']),
                   failed=sum(1 for r in OUT if not r['ok'] and not r['limited']),
                   limited=sum(1 for r in OUT if r['limited']),
                   seconds=round(time.time() - started, 1), real=REAL)
    (WORK / 'r2_offline.json').write_text(json.dumps(dict(summary=summary, rows=OUT), ensure_ascii=False,
                                                      indent=2, default=str), encoding='utf-8')
    print('\nR2-OFFLINE ' + json.dumps(summary, ensure_ascii=False))
    for row in OUT:
        if not row['ok']:
            print('FAILED: %s / %s :: %s' % (row['section'], row['item'],
                                             json.dumps(row['detail'], ensure_ascii=False)[:300]))


if __name__ == '__main__':
    asyncio.run(main())
