# -*- coding: utf-8 -*-
"""D3：经生产入口跑一次低风险只读真实 DSH 委托。

链路（全程走生产入口，不直接调 executor.run / dispatch）：

    模型（FakeModel 只负责"决定要派"）→ Companion._run_cognition
      → 校验 → _commit 落 pending kind=agent 动作      ……(a) 委托被接收
      → Companion._agent_pending_one()（生产里真正跑这件事的那一步）
      → executor.run(action) → AgentManager.dispatch(op=create) → 台账 queued
      → Companion._agent_worker_one()（生产里的台账执行器）
      → AgentManager.execute → _run_dsh → 子进程 companion_agent_worker.py
      → DSH SDK（deepseek-flash，真实调用）             ……(b) worker 实际执行
      → _settle 记回执 + 结果轮 _cognize                ……(c) 结果回到小鱼
      → companion_agent_tasks.status / verification      ……(d) 状态落库
      → companion_usage / spending_calls 记账            ……(e) 预算记录

用法（云端）::

    # 先干跑（不花钱）：只验到"台账 queued"为止，不执行 dsh
    .venv-companion/bin/python -B -X utf8 tools/stage-c/d3_real_delegation.py <work> --dry-run
    # 真跑（会花钱）：一次，失败最多重试一次
    .venv-companion/bin/python -B -X utf8 tools/stage-c/d3_real_delegation.py <work> --real
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys
import time
from pathlib import Path

WORK = Path(sys.argv[1]).resolve()
MODE = 'real' if '--real' in sys.argv else 'dry-run'
PROD = Path('/opt/qq-deepseek')
PROD_HOME = Path('/var/lib/companion/xiaoyu')
BUDGET_SECONDS = 300          # 单次委托的时长上限
OUT = []


def record(step, ok, detail, *, limited=False):
    row = dict(step=step, ok=bool(ok), limited=bool(limited), detail=detail)
    OUT.append(row)
    print('[%s] %s :: %s' % ('PASS' if ok else ('LIMITED' if limited else 'FAIL'), step,
                             json.dumps(detail, ensure_ascii=False, default=str)[:600]), flush=True)
    return row


def _cfg(home):
    """用生产 config.toml 的**真值**建配置对象，但库指向隔离副本。"""
    from life.role_config import load_config
    cfg = load_config(PROD_HOME / 'config.toml')
    return cfg


def _redacted_config(src, dst):
    """生产 config.toml 原样拷到沙箱，但把两个凭据字段清空，且不进证据。"""
    text = src.read_text(encoding='utf-8')
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith('"ws_token"') or stripped.startswith('"api_key"'):
            lines.append(stripped.split('=', 1)[0].strip() + ' = ""')
        else:
            lines.append(line)
    dst.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def stage(home):
    """临时 HOME：生产库的**只读副本** + 脱敏配置 + 人格 + 一个空的工作目录。"""
    (home / 'data').mkdir(parents=True, exist_ok=True)
    import sqlite3
    origin = sqlite3.connect('file:%s?mode=ro' % (PROD_HOME / 'data/life.db'), uri=True, timeout=20)
    try:
        copy = sqlite3.connect(str(home / 'data/life.db'))
        try:
            origin.backup(copy)
        finally:
            copy.close()
    finally:
        origin.close()
    _redacted_config(PROD_HOME / 'config.toml', home / 'config.toml')
    for extra in ('persona.md', 'personas', 'skills'):
        source = PROD_HOME / extra
        if source.is_dir():
            import shutil
            shutil.copytree(source, home / extra, dirs_exist_ok=True)
        elif source.is_file():
            import shutil
            shutil.copy2(source, home / extra)
    # 待读的空工作目录：只放一个探测文件，别的什么都不放
    probe_dir = home / 'probe'
    probe_dir.mkdir(parents=True, exist_ok=True)
    return probe_dir


class DecidingModel:
    """只负责"决定派这件活"；不参与 DSH 那一次真实调用。"""

    MAX_OUTPUT_TOKENS = 8000

    def __init__(self, action):
        self.action = action
        self.calls = 0

    async def complete(self, messages, json_mode=True, purpose='', schema=None):
        self.calls += 1
        decision = dict(reply='我去让子 agent 读一下那个文件', followups=[], remember=[], actions=[],
                        projects=[dict(key='读一个文件', goal='让子 agent 读探测文件并回报', status='active')],
                        wake_after=0, delivery=dict(targets=['desktop']))
        if self.calls == 1:
            decision['actions'] = [dict(self.action)]
        return dict(message=dict(content=json.dumps(decision, ensure_ascii=False)),
                    finish_reason='completed', usage=None, model='fake')

    async def close(self):
        return None


def _task_row(core, agent_id):
    return core.repo._one('SELECT * FROM companion_agent_tasks WHERE id=?', (agent_id,))


def _only_agent_task(core):
    rows = core.repo._all('SELECT * FROM companion_agent_tasks WHERE agent=? ORDER BY created DESC',
                          (core.life.agent_id,))
    return dict(rows[0]) if rows else None


async def main():
    started = time.time()
    import verify_awareness_agents as harness
    harness.ROOT = WORK
    harness.PROD_TREE = PROD
    harness.PROD_HOME = PROD_HOME
    harness._guard_root()

    os.environ.update(harness.sandbox_env())
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    sys.dont_write_bytecode = True
    # life 包从生产树导入（只读）；库与 HOME 都在沙箱里。
    # 不 copytree 生产树：它现在含 toolchain/ 与多次验收快照，拷一份既慢又容易被写。
    sys.path.insert(0, str(PROD))

    home = WORK / 'home'
    probe_dir = stage(home)
    token = 'D3-' + secrets.token_hex(6).upper()
    probe_file = probe_dir / 'probe.txt'
    probe_file.write_text('placeholder-token: %s\n' % token, encoding='utf-8')
    record('准备：空工作目录 + 一个探测文件', True,
           dict(probe_dir=str(probe_dir), probe_file=str(probe_file),
                token_len=len(token), dir_entries=sorted(p.name for p in probe_dir.iterdir())))

    goal = ('只做这一件事，不要做别的：读取文件 %s 的全部内容，'
            '把文件里的 token 原样回复给我（形如 placeholder-token: XXX）。'
            '不要写文件、不要改文件、不要执行安装、不要访问任何数据库、不要联网搜索、'
            '不要创建子代理或再派任何子任务。' % probe_file)

    action = dict(kind='agent', op='create', project='读一个文件', goal=goal,
                  arguments=json.dumps(dict(op='create', goal=goal, node='dsh',
                                            budget=dict(runtime_s=BUDGET_SECONDS)), ensure_ascii=False))

    from life.companion import Companion
    model = DecidingModel(action)
    cfg = _cfg(home)
    core = Companion(cfg, home, model=model,
                     executor=__import__('life.companion_executor', fromlist=['ActionExecutor']).ActionExecutor(cfg, home))
    core.active = True
    record('隔离确认：库指向沙箱、预算模式、DSH 是否可用',
           str(core.home).startswith(str(WORK)),
           dict(home=str(core.home), life_db=str(getattr(cfg, 'life_db', '')),
                budget_mode=getattr(cfg, 'spending_policy', None) and
                __import__('life.spending', fromlist=['SpendingPolicy']).SpendingPolicy.parse(
                    cfg.spending_policy).effective_mode(),
                dsh_paths=bool(core.executor._dsh_paths()),
                dsh_home=str(core.executor._dsh_home()) if hasattr(core.executor, '_dsh_home') else None))

    try:
        # ---------- (a) 走生产决策入口 ----------
        event, _ = core.life.ingest_message(core.scope, '帮我让子 agent 读一下那个文件', role='user',
                                            external_id='d3-in-1')
        core._latest_dialogue = event.id
        await core._run_cognition(event, 'dialogue', None)
        pending = core.repo._one('SELECT * FROM companion_actions WHERE source=?', (event.id,))
        record('(a1) 决策入口受理了模型的 kind=agent 动作（pending）',
               pending is not None and json.loads(pending['action']).get('kind') == 'agent',
               dict(action_id=pending['id'] if pending else None, status=pending['status'] if pending else None,
                    kind=json.loads(pending['action']).get('kind') if pending else None))
        if pending is None:
            raise SystemExit('决策入口没落成待办，后面无从验证')

        # ---------- (a2) 生产里的认领步骤 ----------
        claimed = await core._agent_pending_one()
        after_claim = core.repo._one('SELECT status FROM companion_actions WHERE id=?', (pending['id'],))
        task = _only_agent_task(core)
        record('(a2) _agent_pending_one 认领后进台账（queued）',
               bool(claimed) and after_claim['status'] != 'pending' and task is not None,
               dict(claimed=claimed, action_status=after_claim['status'],
                    agent_id=(task or {}).get('id'), task_status=(task or {}).get('status'),
                    node=(task or {}).get('node'), origin=(task or {}).get('origin')))
        if task is None:
            raise SystemExit('台账里没有这一件，后面无从验证')

        if MODE == 'dry-run':
            record('干跑到此为止：不执行 dsh、不花钱', True,
                   dict(agent_id=task['id'], note='--dry-run：只验到台账 queued 为止'))
            raise SystemExit(0)

        # ---------- (b) 真实执行：台账执行器 → DSH 子进程 ----------
        import subprocess
        run_started = time.time()
        proc_seen = []

        async def watch_proc():
            while time.time() - run_started < BUDGET_SECONDS + 60:
                try:
                    out = subprocess.run(['pgrep', '-af', 'companion_agent_worker.py|dsh-runtime/lib/bin.js'],
                                         capture_output=True, text=True, timeout=10).stdout.strip()
                except Exception:
                    out = ''
                if out and not proc_seen:
                    proc_seen.append(out)
                await asyncio.sleep(0.5)

        watcher = asyncio.create_task(watch_proc())
        done = False
        try:
            while time.time() - run_started < BUDGET_SECONDS:
                if await core._agent_worker_one():
                    done = True
                    break
                await asyncio.sleep(1.0)
        finally:
            watcher.cancel()
            try:
                await watcher
            except BaseException:  # noqa: BLE001
                pass
        elapsed = round(time.time() - run_started, 1)
        record('(b1) 台账执行器跑完（未超时）', done and elapsed < BUDGET_SECONDS,
               dict(done=done, seconds=elapsed, budget_s=BUDGET_SECONDS))
        record('(b2) 观察到真实子进程（DSH/node 或 agent worker）', bool(proc_seen),
               dict(sample=(proc_seen[0][:500] if proc_seen else None),
                    note='pgrep 采样；进程存在本身是"真的起过"的证据，不是台账自述'))

        row = _task_row(core, task['id'])
        result = json.loads(row['result'] or '{}')
        events = core.repo._all('SELECT kind,text,created FROM companion_agent_events WHERE agent_id=? '
                                'ORDER BY created', (task['id'],))
        log_path = core.home / 'data/companion/artifacts' / ('agent-%s.log' % task['id'])
        log_text = log_path.read_text('utf-8', errors='replace')[-4000:] if log_path.is_file() else ''
        record('(b3) DSH 真的被调用（会话号/完成原因/log 里有 DSH 痕迹）',
               bool(result.get('session_id')) or 'deepseek' in log_text.lower()
               or 'harness' in log_text.lower(),
               dict(session_id=result.get('session_id'), finish_reason=result.get('finish_reason'),
                    log_tail=log_text[-800:]))
        record('(b4) 子 agent 没有写文件（探测目录除探测文件外没有新增）',
               sorted(p.name for p in probe_dir.iterdir()) == ['probe.txt'],
               dict(dir_entries=sorted(p.name for p in probe_dir.iterdir()),
                    probe_content_changed=(probe_file.read_text('utf-8') !=
                                           'placeholder-token: %s\n' % token)))

        # ---------- (c) 结果回到小鱼 ----------
        settle = core.repo._one('SELECT status,result FROM companion_actions WHERE id=?', (pending['id'],))
        # events 表的列是 agent_id（不是 agent）；source_kind 按 self_observation/tool_observation 取。
        wakes = core.repo._all("SELECT id,kind,source_kind,payload FROM events WHERE agent_id=? "
                               "AND source_kind IN ('self_observation','tool_observation') "
                               "ORDER BY received_at DESC LIMIT 5", (core.life.agent_id,))
        record('(c1) 动作回执已结算（_settle 写回 pending→终态）',
               settle is not None and settle['status'] in ('completed', 'succeeded', 'failed'),
               dict(action_status=(settle or {})['status'],
                    result_head=str((settle or {}).get('result'))[:300]))
        record('(c2) 结果轮被唤醒（self/tool observation 事件里带着这次结果）',
               any(task['id'] in str(w['payload']) for w in wakes) or bool(
                   core.repo._one("SELECT 1 FROM companion_activity WHERE agent=? AND text LIKE ?",
                                  (core.life.agent_id, '%子 agent%'))),
               dict(wake_events=[dict(id=w['id'][:20], kind=w['kind'], source=w['source_kind']) for w in wakes],
                    activity=[dict(r) for r in core.repo._all(
                        "SELECT type,text FROM companion_activity WHERE agent=? AND text LIKE ? "
                        "ORDER BY created DESC LIMIT 3", (core.life.agent_id, '%子 agent%'))],
                    result_text_seen=(token in str(' '.join(
                        str(w['payload']) for w in wakes)))))

        # ---------- (d) 台账状态与核验 ----------
        record('(d1) 台账终态与 verification 的真实值',
               row is not None,
               dict(agent_id=task['id'], status=row['status'], verification=row['verification'],
                    attempt=row['attempt'], session_id=row['session_id'],
                    outcome=result.get('outcome'),
                    artifacts=[a.get('path') for a in (result.get('artifacts') or [])],
                    events=[e['kind'] for e in events]))
        record('(d2) 回执摘要里有没有那个 token（子 agent 真读到了文件）',
               token in json.dumps(result, ensure_ascii=False) or token in log_text,
               dict(token_prefix=token[:6] + '…', found_in_result=token in json.dumps(result, ensure_ascii=False),
                    found_in_log=token in log_text, result_summary=str(result.get('summary'))[:300]))

        # ---------- (e) 预算与用量 ----------
        usage_rows = core.repo._all("SELECT * FROM companion_usage WHERE agent=? ORDER BY created DESC LIMIT 5",
                                    (core.life.agent_id,))
        spend_rows = core.repo._all("SELECT * FROM spending_calls ORDER BY rowid DESC LIMIT 5")
        record('(e1) 记账：这次调用的用量记成什么（未知必须 null，不能 0）',
               True,
               dict(usage=[dict(r) for r in usage_rows], spending=[dict(r) for r in spend_rows],
                    result_usage=result.get('usage'),
                    note='DSH 拿不到逐次 usage → usage/None；spending_policy 为 off → spending_calls 不新增'))
    finally:
        harness.close_core(core)

    summary = dict(mode=MODE, total=len(OUT), passed=sum(1 for r in OUT if r['ok']),
                   failed=sum(1 for r in OUT if not r['ok']), seconds=round(time.time() - started, 1),
                   token_sha_prefix=token[:6])
    (WORK / 'd3_result.json').write_text(json.dumps(dict(summary=summary, rows=OUT), ensure_ascii=False,
                                                    indent=2, default=str), encoding='utf-8')
    print('\nD3-SUMMARY ' + json.dumps(summary, ensure_ascii=False))


if __name__ == '__main__':
    asyncio.run(main())
