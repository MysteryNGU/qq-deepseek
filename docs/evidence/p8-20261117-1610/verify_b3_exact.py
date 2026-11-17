"""B3 的等价探针：按 agent_id 逐层派，验「第 3 层该被拒」，并对照按名字派的行为。

t9 那行判定用的是 parent['task_ref']（项目名），修复后按名字派在「同名多件」时
会明确拒绝（parent_ambiguous）——那是正确行为，不是失败。真正的上限判定要看按
agent_id 逐层派时：0 → 1 → 2 合法，第 3 层被 nesting_limit 拦下。
"""
import asyncio
import json
import os
import sys
import time

ROOT = sys.argv[1]
PROD = '/opt/qq-deepseek'
sys.path.insert(0, PROD + '/tools/stage-c')
sys.path.insert(0, PROD)
sys.path.insert(0, os.path.join(ROOT, 'tree'))
os.environ.update(dict(HOME=ROOT + '/home', TMPDIR=ROOT + '/tmp', PYTHONPATH=ROOT + '/tree',
                       QQ_OFFLINE='1', COMPANION_FORBID_EXEC='1', PYTHONDONTWRITEBYTECODE='1'))
sys.dont_write_bytecode = True

import verify_awareness_agents as harness  # noqa: E402

harness.ROOT = harness.Path(ROOT).resolve()
harness.PROD_TREE = harness.Path(PROD)
harness.PROD_HOME = harness.Path('/var/lib/companion/xiaoyu')
harness._guard_root()

OUT = []


def record(item, ok, detail):
    OUT.append(dict(item=item, ok=bool(ok), detail=detail))
    print('[%s] %s :: %s' % ('PASS' if ok else 'FAIL', item,
                             json.dumps(detail, ensure_ascii=False, default=str)[:420]), flush=True)


async def main():
    started = time.time()
    os.environ.update(harness.sandbox_env())
    tree = harness.stage_tree()
    sys.path.insert(0, str(tree))
    os.chdir(tree)
    from life import companion_agents as agents_module
    harness.apply_exception_review(agents_module)

    home = harness.stage_home(fresh=True, label='home-b3')
    core = harness.new_core(home)
    manager = core.agents
    try:
        async def spawn(goal, key, parent_id=None, parent_ref=None):
            fields = dict(kind='agent', op='create', node='echo',
                          goal=json.dumps(dict(stdout=goal), ensure_ascii=False),
                          idempotency_key=key, conversation_ref='qq:private:10001')
            if parent_id:
                fields['parent_task_ref'] = parent_id
            if parent_ref:
                fields['parent_task_ref'] = parent_ref
            return await manager.dispatch(fields)

        depth0 = await spawn('零层', 'b3-d0')
        depth1 = await spawn('一层', 'b3-d1', parent_id=depth0['agent_id'])
        depth2 = await spawn('二层', 'b3-d2', parent_id=depth1['agent_id'])
        over = await harness._raises_async(lambda: spawn('三层', 'b3-d3', parent_id=depth2['agent_id']))
        depths = [manager._row(row['agent_id'])['depth'] for row in (depth0, depth1, depth2)]
        record('按 agent_id 逐层派：0 → 1 → 2 都落得下，深度如实记账',
               depths == [0, 1, 2], dict(depths=depths))
        record('第 3 层被 nesting_limit 明确拒绝（不是悄悄放行）',
               'nesting_limit' in str(over), dict(error=over, limit=manager._limits()['max_depth']))
        # 对照：按「项目名」派且同名下有多件未结束的子 agent → 明确拒绝，不靠猜
        by_name = await harness._raises_async(lambda: spawn('按名字', 'b3-name', parent_ref=depth0['task_ref']))
        record('按项目名派且有同名兄弟时：明确 parent_ambiguous（不猜、也不算越层）',
               'parent_ambiguous' in str(by_name), dict(error=by_name))
        # 唯一父时按名字派仍然可用（不误伤正常用法）
        solo = await spawn('独苗', 'b3-solo')
        solo_child = await spawn('独苗的子', 'b3-solo-child', parent_ref=solo['task_ref'])
        record('唯一父时按项目名派仍然可用（没误伤）',
               manager._row(solo_child['agent_id'])['depth'] == 1,
               dict(depth=manager._row(solo_child['agent_id'])['depth'], child=solo_child['agent_id']))
    finally:
        harness.close_core(core)

    summary = dict(total=len(OUT), passed=sum(1 for r in OUT if r['ok']),
                   failed=sum(1 for r in OUT if not r['ok']), seconds=round(time.time() - started, 1))
    (harness.ROOT / 'b3_result.json').write_text(json.dumps(dict(summary=summary, rows=OUT), ensure_ascii=False,
                                                             indent=2, default=str), encoding='utf-8')
    print('\nB3-SUMMARY ' + json.dumps(summary, ensure_ascii=False))


if __name__ == '__main__':
    asyncio.run(main())
