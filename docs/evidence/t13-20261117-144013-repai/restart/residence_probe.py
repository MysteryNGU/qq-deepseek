"""重启后的真实性自检：在隔离临时 HOME + 生产库副本上，用真 Companion 走一遍。

检查的正是「上线后 F2 可不可达」：模型动作 → executor.run → 台账 → inspect 读回；
后台执行器单步；预算三模式在真实策略对象上的行为；dsh 守卫；state 里的 agent_tasks。
不连真实模型、不写生产库、不发 QQ。
"""
import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, '/opt/qq-deepseek')
PROD = Path('/var/lib/companion/xiaoyu')


class FakeModel:
    MAX_OUTPUT_TOKENS = 8000

    def __init__(self):
        self.calls = 0

    async def complete(self, messages, json_mode=True, purpose='', schema=None):
        self.calls += 1
        return dict(message=dict(content=json.dumps(dict(reply='', followups=[], remember=[],
                                                         projects=[], actions=[], wake_after=0))),
                    finish_reason='completed', usage=None, model='fake')

    async def close(self):
        return None


async def main():
    from types import SimpleNamespace
    from life.companion import Companion, DECISION
    from life.spending import SpendingPolicy

    home = Path(tempfile.mkdtemp(prefix='t13-residence-'))
    (home / 'data').mkdir(parents=True)
    shutil.copy2(PROD / 'data/life.db', home / 'data/life.db')
    for extra in ('persona.md', 'personas'):
        src = PROD / extra
        if src.is_dir():
            shutil.copytree(src, home / extra, dirs_exist_ok=True)
        elif src.is_file():
            shutil.copy2(src, home / extra)

    from life.role_config import load_config
    cfg = load_config(PROD / 'config.toml')
    cfg.life_db = 'data/life.db'
    core = Companion(cfg, home, model=FakeModel())
    core.active = True
    out = {}
    try:
        out['companion_module'] = core.__class__.__module__
        out['executor_wired_to_ledger'] = core.executor._agents_manager() is core.agents
        out['agent_in_decision_enum'] = 'agent' in DECISION['properties']['actions']['items'][
            'properties']['kind']['enum']
        receipt = await core.executor.run(dict(kind='agent', op='create', node='echo', project='上线自检',
                                               goal=json.dumps(dict(stdout='通了'), ensure_ascii=False)))
        out['create_receipt'] = {k: receipt.get(k) for k in
                                 ('status', 'agent_id', 'task_ref', 'operation_id')}
        view = await core.executor.run(dict(kind='agent', op='inspect', agent_id=receipt['agent_id']))
        out['inspect_reads_back'] = (view.get('agent_id') == receipt['agent_id']
                                     and view.get('status') == 'queued')
        out['worker_step_available'] = hasattr(core, '_agent_worker_one')
        ran = await core._agent_worker_one() if out['worker_step_available'] else None
        row = core.agents._row(receipt['agent_id'])
        out['worker_step'] = dict(ran=ran, state=row['status'], verification=row['verification'])
        modes = {}
        for label, pol in (
                ('off', SpendingPolicy.parse(dict(enabled=False))),
                ('bounded_estimate', SimpleNamespace(**{**SpendingPolicy.parse(dict(enabled=True)).__dict__,
                                                        'mode': 'bounded_estimate'})),
                ('strict', SpendingPolicy.parse(dict(enabled=True)))):
            core.spending.policy = pol
            result = core.agents.create(json.dumps(dict(stdout='x')), node='echo',
                                        idempotency_key='budget-probe-' + label,
                                        conversation_ref='qq:private:10001')
            budget = result.get('budget') or {}
            modes[label] = dict(status=result.get('status'), code=result.get('code'),
                                mode=budget.get('mode'), usage_known=budget.get('usage_known'),
                                accounting=(budget.get('accounting') or {}).get('mode'),
                                reason=str(result.get('reason') or '')[:60])
        out['budget_modes'] = modes
        guard = {}
        for label, pol in (('off', SpendingPolicy.parse(dict(enabled=False))),
                           ('strict', SpendingPolicy.parse(dict(enabled=True)))):
            core.spending.policy = pol
            guard[label] = dict(blocked=core._dsh_budget_blocked(),
                                reason=core._dsh_budget_reason()[:60])
        out['dsh_guard'] = guard
        messages = core._messages(None, '自检', {}, purpose='activity')
        bundle = {}
        for message in messages:
            content = message.get('content')
            if isinstance(content, str) and content.startswith('当前生活材料：'):
                bundle = json.loads(content.split('当前生活材料：', 1)[1])
        out['state_agent_tasks_present'] = 'agent_tasks' in bundle
        out['available_actions'] = bundle.get('available_actions')
    finally:
        core.life.close()
    print(json.dumps(out, ensure_ascii=False))


asyncio.run(main())
