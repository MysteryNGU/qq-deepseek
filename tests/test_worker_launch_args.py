# -*- coding: utf-8 -*-
"""P19/t27 回归：产品 worker 的 dsh launch **必须**带上 --patch。

背景（四轮才查到的根因）：SDK `client.py:79` 是
``args = list(self._launch_args or self._default_launch_args(env))``
——只要传了显式 ``_launch_args``，就**完全绕过** ``_default_launch_args``，
而 ``--patch`` 只在后者里拼。此前 worker 传
``(node, bin.js, '--profile', 'sdk-minimal')``，于是 ``config.patches`` 被静默丢弃，
沙箱补丁从没到过 dsh（运行期实测 defaultMode 一直是 danger-full-access）。

这条测试锁住「launch 里必须出现 --patch <路径>」，防止有人改回去。
不启动 dsh、不跑模型：用 monkeypatch 截获 launch 元组。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class WorkerLaunchArgs(unittest.TestCase):
    def _launch_for(self, request):
        """跑 worker 的 run_dsh，但在构造 Harness 前把 launch 截获下来。"""
        sys.path.insert(0, str(ROOT / 'vendor/dsh-runtime/python/sdk/src'))
        import life.companion_agent_worker as worker

        captured = {}

        class FakeHarness:
            def __init__(self, config, _launch_args=None):
                captured['config'] = config
                captured['launch'] = list(_launch_args or [])

            def start(self):
                return None

            def close(self):
                return None

        real = None
        had_attr = hasattr(worker, 'DeepSeekHarness')
        if had_attr:
            real = worker.DeepSeekHarness
            worker.DeepSeekHarness = FakeHarness
        try:
            # run_dsh 内部 from deepseek_harness import ...，所以还要patch模块
            import deepseek_harness
            real2 = getattr(deepseek_harness, 'DeepSeekHarness', None)
            deepseek_harness.DeepSeekHarness = FakeHarness
            try:
                worker.run_dsh(dict(request, initialize_only=True))
            finally:
                if real2 is not None:
                    deepseek_harness.DeepSeekHarness = real2
        finally:
            if had_attr and real is not None:
                worker.DeepSeekHarness = real
        return captured

    def test_launch_carries_every_patch(self):
        patch_a = '/tmp/t27-test-a.yml'
        patch_b = '/tmp/t27-test-b.yml'
        Path(patch_a).write_text('- id: sandbox-policy\n', encoding='utf-8')
        Path(patch_b).write_text('- id: sandbox-policy\n', encoding='utf-8')
        request = dict(
            dsh_root=str(ROOT / 'vendor/dsh-runtime'),
            node='/opt/qq-deepseek/toolchain/node/bin/node',
            entry=str(ROOT / 'vendor/dsh-runtime/lib/bin.js'),
            dsh_home='/tmp/t27-test-home',
            cwd='/tmp',
            patches=[patch_a, patch_b],
        )
        if not Path(request['node']).is_file() or not Path(request['entry']).is_file():
            self.skipTest('本机没有 dsh 运行时（node/entry 不存在）')
        Path(request['dsh_home']).mkdir(parents=True, exist_ok=True)
        captured = self._launch_for(request)
        launch = captured.get('launch') or []
        self.assertTrue(launch, 'launch 没被截获，测试本身失效')
        # 核心断言：每个 patch 都要以 `--patch <path>` 成对出现（多份补丁都要带）
        pairs = [(launch[i], launch[i + 1]) for i in range(len(launch) - 1)
                 if launch[i] == '--patch']
        self.assertEqual(len(pairs), 2, 'launch 里的 --patch 数量必须等于补丁数量')
        got = sorted(path for _, path in pairs)
        want = sorted(str(Path(path).resolve()) for path in (patch_a, patch_b))
        self.assertEqual(got, want, '--patch 后面必须紧跟对应补丁的真实绝对路径')
        # 同时确认 profile 仍在（别为了加 patch 把 profile 弄丢）
        self.assertIn('--profile', launch)
        self.assertIn('sdk-minimal', launch)

    def test_launch_without_patches_still_valid(self):
        request = dict(
            dsh_root=str(ROOT / 'vendor/dsh-runtime'),
            node='/opt/qq-deepseek/toolchain/node/bin/node',
            entry=str(ROOT / 'vendor/dsh-runtime/lib/bin.js'),
            dsh_home='/tmp/t27-test-home2',
            cwd='/tmp',
            patches=[],
        )
        if not Path(request['node']).is_file() or not Path(request['entry']).is_file():
            self.skipTest('本机没有 dsh 运行时')
        Path(request['dsh_home']).mkdir(parents=True, exist_ok=True)
        launch = (self._launch_for(request).get('launch') or [])
        self.assertTrue(launch)
        self.assertNotIn('--patch', launch, '没有补丁时不该凭空冒出一个 --patch')
        self.assertIn('--profile', launch)


if __name__ == '__main__':
    unittest.main()
