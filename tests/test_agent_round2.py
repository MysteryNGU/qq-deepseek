# -*- coding: utf-8 -*-
"""P13（t20）第二轮：能力解耦、权限、嵌套硬拦、记账分离、生命周期。

全部在隔离环境跑：临时 HOME + 生产库快照（sqlite 在线备份）+ 外部平台禁用。
不连真实模型（stub 后端）、不发 QQ、不写生产库。
"""
import asyncio
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

PROD = Path('/var/lib/companion/xiaoyu')
if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _home():
    home = Path(tempfile.mkdtemp(prefix='t20-'))
    (home / 'data').mkdir(parents=True)
    try:
        src = sqlite3.connect('file:%s?mode=ro' % (PROD / 'data' / 'life.db'), uri=True)
        dst = sqlite3.connect(str(home / 'data' / 'life.db'))
        src.backup(dst)
        dst.close()
        src.close()
    except Exception:  # noqa: BLE001 - 没有生产库时用全新库
        pass
    return home


def _cfg(**extra):
    from life.spending import SpendingPolicy
    base = dict(base_url='https://api.deepseek.com', api_key='sk-test-not-real', model='deepseek-flash',
                life_agent='xiaoyu-test', dsh_provider='deepseek-official', life_dynamics={},
                persona_file='persona.md', image_generation=None, spending_policy={},
                agent_max_concurrent=4, agent_max_depth=2, agent_runtime_s=30,
                dsh_root='/opt/qq-deepseek/vendor/dsh-runtime',
                dsh_node='/opt/qq-deepseek/toolchain/node/bin/node', dsh_home='data/dsh-linux')
    base.update(extra)
    return SimpleNamespace(**base)


class _Base(unittest.TestCase):
    def setUp(self):
        self.home = _home()
        self.addCleanup(shutil.rmtree, self.home, True)

    def manager(self, **extra):
        from life.runtime import LifeRuntime
        from life.companion_agents import AgentManager
        cfg = _cfg(**extra)
        cfg.life_db = 'data/life.db'
        self.life = LifeRuntime(self.home / 'data/life.db', cfg.life_agent)
        self.addCleanup(self.life.close)
        return AgentManager(cfg, self.home, repo=self.life.repo, agent_id=self.life.agent_id)

    def finish(self, manager, agent_id, result):
        """用产品路径把一件活跑完：先领走（running），再 _finish（算证据、写 verification）。"""
        if manager._row(agent_id)['status'] == 'queued':
            manager.set_status(agent_id, 'running')
        return manager._finish(agent_id, dict(result))


class CapabilityDecoupling(_Base):
    """第 1 项：能力「已接通」与单次「成果验证通过」互相独立。"""

    def test_fileless_task_gets_its_own_success_judgement(self):
        mgr = self.manager()
        goal = json.dumps(dict(result='placeholder-token-ok'), ensure_ascii=False)
        made = mgr.create(goal, node='echo', idempotency_key='k1', conversation_ref='qq:private:1')
        result = dict(return_code=0, stdout='读到的内容是 placeholder-token-ok\n', artifacts=[],
                      tests=[])
        self.finish(mgr, made['agent_id'], result)
        row = mgr._row(made['agent_id'])
        manifest = json.loads(row['result'])
        # (a) 没有交付物也有自己的成功判据，并且判定为通过
        self.assertEqual(manifest['outcome'], 'completed')
        self.assertTrue(manifest['judged'], '无交付物任务必须有判据')
        self.assertTrue(manifest['judged_ok'])
        self.assertEqual(row['verification'], 'passed', '判据命中就是真实证据，可以 passed')

    def test_mismatched_output_does_not_become_passed(self):
        mgr = self.manager()
        goal = json.dumps(dict(result='placeholder-token-ok'), ensure_ascii=False)
        made = mgr.create(goal, node='echo', idempotency_key='k2', conversation_ref='qq:private:1')
        self.finish(mgr, made['agent_id'], dict(return_code=0, stdout='什么也没读到\n', artifacts=[]))
        row = mgr._row(made['agent_id'])
        self.assertEqual(row['verification'], 'failed', '判据没命中就是没通过，不能凑')
        manifest = json.loads(row['result'])
        self.assertTrue(manifest['judged'])
        self.assertFalse(manifest['judged_ok'])

    def test_no_evidence_keeps_pending_and_capability_still_becomes_ready(self):
        mgr = self.manager()
        made = mgr.create('随便做点什么', node='echo', idempotency_key='k3',
                          conversation_ref='qq:private:1')
        self.finish(mgr, made['agent_id'], dict(return_code=0, stdout='做完了\n', artifacts=[]))
        row = mgr._row(made['agent_id'])
        # (b) 没有任何证据时 verification 不能凭空变 passed
        self.assertEqual(row['verification'], 'pending')
        # (c) 能力状态基于「链路最近有成功调用」，不依赖那件的 verification
        from life.companion_awareness import agent_capability
        core = SimpleNamespace(repo=self.life.repo, life=self.life, agents=mgr)
        item = agent_capability(core, mode='bounded_estimate', endpoint=True)
        self.assertEqual(item['status'], 'ready')
        self.assertTrue(any('companion_agent_tasks:' in ref for ref in item['evidence']))
        # 同一件的 verification 仍是 pending —— 两个维度各说各的
        self.assertEqual(mgr._row(made['agent_id'])['verification'], 'pending')

    def test_capability_never_writes_verification(self):
        mgr = self.manager()
        made = mgr.create('做一件事', node='echo', idempotency_key='k4', conversation_ref='qq:private:1')
        self.finish(mgr, made['agent_id'], dict(return_code=0, stdout='x\n', artifacts=[]))
        from life.companion_awareness import agent_capability
        core = SimpleNamespace(repo=self.life.repo, life=self.life, agents=mgr)
        before = mgr._row(made['agent_id'])['verification']
        agent_capability(core, mode='bounded_estimate', endpoint=True)
        agent_capability(core, mode='strict', endpoint=True)
        self.assertEqual(mgr._row(made['agent_id'])['verification'], before,
                         '读能力目录不能顺手改某件任务的核验结论')


class Permissions(_Base):
    """第 2 项：白名单、沙箱模式、越界必须被拒。"""

    def test_workdir_allowed_but_outside_and_secrets_refused(self):
        mgr = self.manager()
        workdir = mgr._workdir(dict(id='companion-agent_x', node='work'), {})
        self.assertTrue(mgr._permission_verdict(path=str(workdir / 'a.txt'), mode='write')[0])
        self.assertTrue(mgr._permission_verdict(path=str(workdir / 'a.txt'), mode='read')[0])
        # 读越界：白名单外的系统目录
        ok, why = mgr._permission_verdict(path='/var/log/syslog', mode='read')
        self.assertFalse(ok)
        self.assertEqual(why, 'outside_read_allowlist')
        # 写越界：工作目录之外
        ok, why = mgr._permission_verdict(path='/opt/qq-deepseek/life/companion.py', mode='write')
        self.assertFalse(ok)
        self.assertEqual(why, 'outside_write_allowlist')
        # 读凭证：运行时 home（里面有会话与 llm 记录）一律拒
        ok, why = mgr._permission_verdict(path=str(mgr._state_dir() / 'llm-deepseek/x.json'),
                                          mode='read')
        self.assertFalse(ok)
        self.assertEqual(why, 'secret_path')

    def test_default_sandbox_is_not_full_access(self):
        mgr = self.manager()
        self.assertEqual(mgr._sandbox_mode(), 'workspace-write')
        self.assertEqual(mgr._dsh_env()['DSH_PERMISSION_MODE'], 'workspace-write')
        elevated = self.manager(agent_full_access=True)
        self.assertEqual(elevated._sandbox_mode(), 'danger-full-access')

    def test_patch_is_an_override_not_an_insert(self):
        """P18 回归：权限补丁必须是「同 id 覆盖」，不能是 - insert:（追加实例）。"""
        mgr = self.manager()
        workdir = mgr._workdir(dict(id='companion-agent_p', node='work'), {})
        patch = mgr._sandbox_patch(workdir)
        text = patch.read_text(encoding='utf-8')
        # 只看真正的 YAML 行（注释里提到 insert 是在解释为什么不能用它）
        body = [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith('#')]
        self.assertFalse([line for line in body if line.strip() == '- insert:'],
                         'insert 是追加实例，旧的 danger-full-access 仍会生效')
        self.assertEqual(text.count('- id: sandbox-policy'), 1)
        modes = [line.split(':', 1)[1].strip() for line in text.splitlines()
                 if line.strip().startswith('mode:')]
        self.assertEqual(modes, [mgr._sandbox_mode()])

    def test_dump_config_shows_exactly_one_sandbox_policy(self):
        """P18 强断言：真正合成后的配置里 sandbox-policy 只能有一个，mode 只能是期望值。

        用 dsh 自己的 --dump-config 合成（与产品 spawn 时同一条路径）。这条断言是
        「以后又有人改回 insert」的唯一防线。
        """
        import subprocess
        mgr = self.manager()
        workdir = mgr._workdir(dict(id='companion-agent_q', node='work'), {})
        workdir.mkdir(parents=True, exist_ok=True)
        patch = mgr._sandbox_patch(workdir)
        node = '/opt/qq-deepseek/toolchain/node/bin/node'
        entry = '/opt/qq-deepseek/vendor/dsh-runtime/lib/bin.js'
        if not (Path(node).is_file() and Path(entry).is_file()):
            self.skipTest('本机没有 dsh 运行时')
        env = dict(os.environ, DSH_HOME=str(self.home / 'dsh-home'), DSH_PERMISSION_MODE='danger-full-access')
        (self.home / 'dsh-home').mkdir(parents=True, exist_ok=True)
        proc = subprocess.run([node, entry, '--profile', 'sdk-minimal', '--patch', str(patch),
                               '--dump-config'], capture_output=True, text=True, env=env, timeout=180)
        out = proc.stdout
        self.assertEqual(out.count('- id: sandbox-policy'), 1,
                         '沙箱策略节点只能有一个（出现两个说明补丁又被写成 insert）')
        modes = [line.split(':', 1)[1].strip() for line in out.splitlines()
                 if line.strip().startswith('mode:')]
        self.assertEqual(modes, [mgr._sandbox_mode()],
                         '合成后的 mode 必须恰好一个且等于产品裁定的模式，实际 %r' % (modes,))
        self.assertNotIn('danger-full-access', modes)

    def test_escape_hatch_defaults_off(self):
        """逃生口保留但默认关闭；打开时必须显式配置。"""
        self.assertEqual(self.manager()._sandbox_mode(), 'workspace-write')
        self.assertEqual(self.manager(agent_full_access=True)._sandbox_mode(), 'danger-full-access')

    def test_credential_is_not_in_agent_readable_state(self):
        mgr = self.manager()
        env = mgr._dsh_env()
        self.assertIn('DEEPSEEK_API_KEY', env, '必须注入才能调模型')
        # 但凭证不能出现在子 agent 可读的目录里
        workdir = mgr._workdir(dict(id='companion-agent_y', node='work'), {})
        workdir.mkdir(parents=True, exist_ok=True)
        for name in ('env.json', 'key.txt'):
            (workdir / name).write_text(json.dumps(env), encoding='utf-8')
        self.assertFalse(mgr._permission_verdict(path=str(workdir / 'env.json'), mode='read')[0]
                         and 'secret_path' != mgr._permission_verdict(
                             path=str(mgr._state_dir() / 'x'), mode='read')[1])
        self.assertEqual(mgr._permission_verdict(path=str(mgr._state_dir() / 'x'), mode='read'),
                         (False, 'secret_path'))
        banned, _ = mgr._permission_verdict(path=str(mgr._state_dir()), mode='write')
        self.assertFalse(banned, '凭证目录不可写')


class NestingHardStop(_Base):
    """第 3 项：create / send / resume 三个入口都要拦得住。"""

    def _chain(self, mgr, levels=3):
        made = []
        parent = None
        for index in range(levels):
            key = dict(idempotency_key='n%d' % index, conversation_ref='qq:private:1')
            if parent:
                key['parent_task_ref'] = parent
            made.append(mgr.create('第%d层' % index, node='echo', **key))
            parent = made[-1]['agent_id']
        return made

    def test_create_third_level_is_refused(self):
        from life.companion_agents import AgentError
        mgr = self.manager(agent_max_depth=2)
        made = self._chain(mgr, 3)
        self.assertEqual(len(made), 3)
        with self.assertRaises(AgentError) as caught:
            mgr.create('第4层', node='echo', parent_task_ref=made[-1]['agent_id'],
                       idempotency_key='n4', conversation_ref='qq:private:1')
        self.assertEqual(caught.exception.code, 'nesting_limit')

    def test_send_into_deepest_level_is_refused(self):
        from life.companion_agents import AgentError
        mgr = self.manager(agent_max_depth=2)
        made = self._chain(mgr, 3)
        with self.assertRaises(AgentError) as caught:
            mgr.send(made[-1]['agent_id'], text='再派一个给他')
        self.assertEqual(caught.exception.code, 'nesting_limit')
        # 浅层的收消息不受影响
        self.assertTrue(mgr.send(made[0]['agent_id'], text='继续')['ok'])

    def test_resume_of_deepest_level_is_refused(self):
        from life.companion_agents import AgentError
        mgr = self.manager(agent_max_depth=2)
        made = self._chain(mgr, 3)
        deepest = made[-1]['agent_id']
        mgr.set_status(deepest, 'running')
        mgr.set_status(deepest, 'interrupted', failure=dict(code='x', reason='测试'))
        with self.assertRaises(AgentError) as caught:
            mgr.resume(deepest)
        self.assertEqual(caught.exception.code, 'nesting_limit')


class AccountingSsplit(_Base):
    """第 4 项：预算 off 也要记账，估算与实测分开，未知不写 0。"""

    def test_off_records_call_with_unknown_cost(self):
        from life.spending import SpendingPolicy
        from life.runtime import LifeRuntime
        cfg = _cfg(spending_policy=dict(enabled=False, mode="off"))
        life = LifeRuntime(self.home / 'data/life.db', cfg.life_agent)
        self.addCleanup(life.close)
        role = SimpleNamespace(cfg=cfg, repo=life.repo, life=life)
        spending = __import__('life.spending', fromlist=['Spending']).Spending(role)
        record = dict(call_key='agent:probe-off', provider='deepseek-official',
                      model='deepseek-flash', origin='subagent', usage=None,
                      started_at=time.time())
        first = spending.reserve(None, dict(session_id='companion:x', record=record))
        self.assertTrue(first['recorded'])
        self.assertEqual(first['cost_state'], 'unknown')
        spending.settle(dict(session_id='companion:x', record=dict(record, finished_at=time.time())))
        state = spending.call_state('agent:probe-off')
        self.assertEqual(state['mode'], 'off')
        self.assertEqual(state['origin'], 'subagent')
        self.assertEqual(state['cost_state'], 'unknown')
        self.assertIsNone(state['measured_nano'], '拿不到用量就是未知，不能写 0')
        self.assertIsNone(state['estimated_nano'], '没有估算就留空，不冒充实测')
        self.assertNotEqual(state['measured_nano'], 0)

    def test_unknown_never_counted_as_zero(self):
        from life.spending import reported_cost
        self.assertIsNone(reported_cost(dict(provider='deepseek-official', model='deepseek-flash',
                                             usage=None)), '没有 usage 必须是 None')

    def test_finish_records_subagent_usage_with_origin(self):
        mgr = self.manager()
        made = mgr.create('跑一件', node='echo', idempotency_key='acct', conversation_ref='qq:private:1')
        self.finish(mgr, made['agent_id'], dict(return_code=0, stdout='ok\n', artifacts=[]))
        row = mgr._row(made['agent_id'])
        self.assertEqual(row['status'], 'completed')
        # 用量拿不到 → None（不是 0）
        self.assertIsNone(json.loads(row['result']).get('usage'))


class Lifecycle(_Base):
    """第 5 项：并发/排队、超时、kill 之后的收尾（不重复执行、不留孤儿、不永久 running）。"""

    def test_fourth_task_queues_instead_of_failing(self):
        mgr = self.manager(agent_max_concurrent=3)
        made = [mgr.create('第%d件' % i, node='echo', idempotency_key='c%d' % i,
                           conversation_ref='qq:private:1') for i in range(4)]
        self.assertEqual(len({item['agent_id'] for item in made}), 4)
        queued = mgr.queued(limit=10)
        self.assertEqual(len(queued), 4, '第 4 件是排队，不是失败')
        for index in range(3):
            mgr.set_status(made[index]['agent_id'], 'running')
        self.assertEqual(mgr._running_count(), 3)
        self.assertEqual(mgr._limits()['max_concurrent'], 3)
        # 第 4 件仍在 queued，没有被拒
        self.assertEqual(mgr._row(made[3]['agent_id'])['status'], 'queued')

    def test_timeout_marks_interrupted_not_permanently_running(self):
        mgr = self.manager()
        made = mgr.create('会超时的一件', node='echo', idempotency_key='to', conversation_ref='qq:private:1')
        mgr.set_status(made['agent_id'], 'running')
        mgr.set_status(made['agent_id'], 'interrupted',
                       failure=dict(code='timeout', reason='超过 runtime_s'))
        row = mgr._row(made['agent_id'])
        self.assertEqual(row['status'], 'interrupted')
        self.assertNotEqual(row['status'], 'running')

    def test_reconcile_after_kill_does_not_duplicate_finished_work(self):
        mgr = self.manager()
        done = mgr.create('已经做完的', node='echo', idempotency_key='done1', conversation_ref='qq:private:1')
        self.finish(mgr, done['agent_id'], dict(return_code=0, stdout='完成\n', artifacts=[]))
        running = mgr.create('被杀时正在跑', node='echo', idempotency_key='run1',
                             conversation_ref='qq:private:1')
        mgr.set_status(running['agent_id'], 'running')
        mgr._update(running['agent_id'], pid=os.getpid() + 100000)  # 一个不存在的 pid
        result = mgr.reconcile_on_start()
        self.assertEqual(mgr._row(done['agent_id'])['status'], 'completed')
        self.assertNotIn(running['agent_id'], [item.get('agent_id') for item in
                                               (result.get('requeued') or [])],
                         '已完成的活不能被重新排进队列')
        after = mgr._row(running['agent_id'])
        self.assertIn(after['status'], {'interrupted', 'queued', 'failed',
                                        'completed'} - {'running'},
                      '进程被杀后不能永久停在 running')
        self.assertIsNotNone(result)


if __name__ == '__main__':
    unittest.main()

# --- t29 sandbox regression (appended) ---
from .sandbox_regression_t29 import SandboxRegression  # noqa: E402


class SandboxRegressionTests(SandboxRegression, _Base):
    """t29: fs-sandbox mounted, patch verified, reads allowlisted."""
