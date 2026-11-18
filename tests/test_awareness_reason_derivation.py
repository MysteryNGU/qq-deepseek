"""P33 第 4 步：写死文案改成读数据源。

验收判据（captain 定的）：**状态正常的夹具上，必须返回 ready / 对应的真实词，
不能返回 needs_setup / offline / unknown。**

覆盖 2026-11-18 实测出的四个「她说错话」的 bug：
  * town 能跑 199 次却报 needs_setup        -> life/companion_awareness.py:503
  * bilibili 三种「没登录」被抹平            -> :506
  * dsh 写死「没有运行时」                   -> :508
  * 手机「90 秒」文案与常量脱节              -> :482 / :673
"""
import unittest
from types import SimpleNamespace

from life import companion_awareness as aw


def _core(**kw):
    base = dict(town=None, executor=None)
    base.update(kw)
    return SimpleNamespace(**base)


class TownReasonTests(unittest.TestCase):
    def test_online_town_is_not_needs_setup(self):
        """核心回归：小镇在跑（204 次动作成功过）时不能再报 needs_setup。"""
        core = _core(town=SimpleNamespace(state='online', _error=''))
        text, scope = aw._town_reason(core)
        self.assertEqual(scope, 'ready', '小镇在线却报 %s，就是今晚那个 bug' % scope)
        self.assertNotIn('没有保存接入码', text)

    def test_disabled_town_is_needs_setup_with_real_reason(self):
        core = _core(town=SimpleNamespace(state='disabled', _error=''))
        text, scope = aw._town_reason(core)
        self.assertEqual(scope, 'needs_setup')
        self.assertIn('没有启用', text)

    def test_missing_account_and_invalid_code_are_distinguished(self):
        a = aw._town_reason(_core(town=SimpleNamespace(state='needs_account', _error='')))
        b = aw._town_reason(_core(town=SimpleNamespace(state='invalid_code', _error='')))
        self.assertEqual(a[1], 'needs_setup')
        self.assertEqual(b[1], 'needs_setup')
        self.assertNotEqual(a[0], b[0], '两种原因必须给出不同的话')

    def test_reconnecting_is_offline_not_needs_setup(self):
        core = _core(town=SimpleNamespace(state='reconnecting', _error='TimeoutError'))
        text, scope = aw._town_reason(core)
        self.assertEqual(scope, 'offline', '连不上是 offline，不是配置问题')
        self.assertIn('TimeoutError', text, '真实错误要带出来')

    def test_unreadable_town_is_unknown_not_a_guess(self):
        core = _core(town=SimpleNamespace(state='some_new_state', _error=''))
        _, scope = aw._town_reason(core)
        self.assertEqual(scope, 'unknown', '没见过的状态不能编原因')

    def test_bridge_missing_is_unknown(self):
        _, scope = aw._town_reason(_core(town=None))
        self.assertEqual(scope, 'unknown')

    def test_bridge_raising_is_unknown(self):
        class Boom:
            @property
            def state(self):
                raise RuntimeError('boom')
        _, scope = aw._town_reason(_core(town=Boom()))
        self.assertEqual(scope, 'unknown', '探测抛错必须是 unknown，不能断言')


class BilibiliReasonTests(unittest.TestCase):
    def _exec(self, status):
        return SimpleNamespace(_bilibili=SimpleNamespace(status=lambda: status))

    def test_available_but_no_credential_says_login_not_plugin(self):
        text, scope = aw._bilibili_reason(
            _core(executor=self._exec(dict(available=True, credential_present=False, connected=False))))
        self.assertEqual(scope, 'needs_setup')
        self.assertIn('登录', text)
        self.assertNotIn('插件没有安装', text, '插件在的时候不能说没装')

    def test_plugin_missing_says_install_not_login(self):
        text, scope = aw._bilibili_reason(
            _core(executor=self._exec(dict(available=False, credential_present=False, connected=False))))
        self.assertEqual(scope, 'needs_setup')
        self.assertIn('插件', text)
        self.assertNotIn('扫码登录', text, '插件没装时不能说去登录')

    def test_fully_working_is_ready(self):
        text, scope = aw._bilibili_reason(
            _core(executor=self._exec(dict(available=True, credential_present=True, connected=True))))
        self.assertEqual(scope, 'ready', '三种原因不能把「其实是好的」也报成不可用')

    def test_credential_but_no_process_is_offline(self):
        _, scope = aw._bilibili_reason(
            _core(executor=self._exec(dict(available=True, credential_present=True, connected=False))))
        self.assertEqual(scope, 'offline', '插件和凭据都在只是进程没起，这是 offline')

    def test_mode_parity_with_old_text(self):
        text, scope = aw._bilibili_reason(_core(executor=self._exec(dict(
            available=True, credential_present=False, connected=False))))
        # 旧文案把「插件未配置或登录已失效」混成一句；现在必须只说登录这一种。
        self.assertNotIn('插件未配置', text)


class DshReasonTests(unittest.TestCase):
    def test_endpoint_present_is_ready(self):
        core = _core(executor=SimpleNamespace(_dsh_paths=lambda: ('/root', '/node', 'x'),
                                              original=None))
        _, scope = aw._dsh_reason(core, executor=core.executor)
        self.assertEqual(scope, 'ready', 'DSH 在的时候不能还写死「没有运行时」')

    def test_endpoint_absent_is_needs_setup(self):
        core = _core(executor=SimpleNamespace(_dsh_paths=lambda: None, original=None))
        _, scope = aw._dsh_reason(core, executor=core.executor)
        self.assertEqual(scope, 'needs_setup')

    def test_probe_failure_is_unknown(self):
        def boom():
            raise RuntimeError('io')
        core = _core(executor=SimpleNamespace(_dsh_paths=boom, original=None))
        _, scope = aw._dsh_reason(core, executor=core.executor)
        self.assertEqual(scope, 'unknown')


class PhoneTextTests(unittest.TestCase):
    def test_no_hardcoded_seconds_in_user_facing_text(self):
        """常量 RECENT_SECONDS 一改，文案不能跟着撒谎。"""
        import io
        source = io.open(aw.__file__, encoding='utf-8').read()
        for bad in ('最近 90 秒内没有上报过注册', '手机在最近 90 秒内注册过'):
            self.assertNotIn(bad, source, '文案里还有写死的秒数：%s' % bad)

    def test_phone_offline_text_keeps_actionable_advice(self):
        core = _core(executor=SimpleNamespace(phone_context=lambda: dict(devices=[])))
        text, scope = aw._offline_reason(core, 'phone', executor=core.executor)
        self.assertEqual(scope, 'offline')
        self.assertIn('打开小鱼 App', text, '删掉秒数不等于删掉可操作建议')


if __name__ == '__main__':
    unittest.main()
