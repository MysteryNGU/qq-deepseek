"""第 1 步事实层判据：形状统一、词表统一、读不到就是 unknown。

判据核心（captain 定的）：
  **状态正常的夹具上，返回 needs_setup / offline 就算违规。**
反向也成立：**读不到的时候返回 ready / 具体不可用，同样是违规**
（把「不知道」说成「知道」，正是 F-2 修的那个 bug）。
"""
import unittest
from types import SimpleNamespace

from life import companion_world as world


class ShapeTests(unittest.TestCase):
    def test_fact_shape_is_fixed(self):
        value = world.fact('phone.link', 'phone', 'ready',
                           source_ref='life/companion_phone.py#context',
                           observed_at=1_700_000_000, detail='ok')
        for key in ('fact_ref', 'subject', 'state', 'source_ref', 'observed_at',
                    'age_s', 'detail', 'raw'):
            self.assertIn(key, value)
        self.assertEqual(value['state'], 'ready')

    def test_state_vocabulary_is_the_existing_one(self):
        self.assertEqual(world.STATES,
                         ('ready', 'needs_setup', 'offline', 'unsupported', 'unknown'))

    def test_invented_state_is_rejected(self):
        """不发明第四套词表：写错词直接报错，而不是悄悄放行。"""
        with self.assertRaises(ValueError):
            world.fact('x.y', 'x', 'starved', source_ref='s')

    def test_fact_requires_ref_and_subject(self):
        with self.assertRaises(ValueError):
            world.fact('', 'phone', 'ready', source_ref='s')
        with self.assertRaises(ValueError):
            world.fact('phone.link', '', 'ready', source_ref='s')

    def test_unknown_says_cannot_confirm_not_unavailable(self):
        value = world.unknown('phone', '探测抛错 TimeoutError', source_ref='s')
        self.assertEqual(value['state'], 'unknown')
        self.assertIn('读不到', value['detail'])
        self.assertIn('不能确认', value['detail'])
        self.assertNotIn('不可用', value['detail'], '读不到不等于不可用')

    def test_brief_drops_raw(self):
        """raw 不进主上下文。"""
        value = world.fact('a.b', 'a', 'ready', source_ref='s', raw={'huge': 'x' * 5000})
        brief = world.brief(value)
        self.assertNotIn('raw', brief)
        self.assertEqual(brief['state'], 'ready')

    def test_brief_all_puts_problems_first(self):
        rows = [world.fact('a', 'a', 'ready', source_ref='s'),
                world.fact('b', 'b', 'needs_setup', source_ref='s'),
                world.unknown('c', 'x', source_ref='s')]
        order = [row['state'] for row in world.brief_all(rows)]
        self.assertEqual(order[0], 'unknown')
        self.assertEqual(order[-1], 'ready')


class PhoneFactTests(unittest.TestCase):
    def test_no_devices_is_offline_not_ready(self):
        value = world.phone_context_fact(dict(devices=[]))
        self.assertEqual(value['state'], 'offline')

    def test_registered_without_stream_is_offline(self):
        value = world.phone_context_fact(dict(devices=[dict(has_stream=False)]))
        self.assertEqual(value['state'], 'offline')

    def test_active_stream_is_ready(self):
        """状态正常必须 ready——返回 offline 就是违规。"""
        value = world.phone_context_fact(dict(devices=[dict(has_stream=True)]))
        self.assertEqual(value['state'], 'ready')

    def test_missing_context_is_unknown_not_offline(self):
        value = world.phone_context_fact(None)
        self.assertEqual(value['state'], 'unknown', '拿不到不能断言手机不可用')

    def test_does_not_mutate_input(self):
        context = dict(devices=[dict(has_stream=True)])
        snapshot = repr(context)
        world.phone_context_fact(context)
        self.assertEqual(repr(context), snapshot)


class ChannelFactTests(unittest.TestCase):
    def test_connected_maps_to_ready(self):
        value = world.channel_fact('qq', dict(state='connected', source_ref='s'))
        self.assertEqual(value['state'], 'ready')

    def test_login_required_maps_to_needs_setup(self):
        value = world.channel_fact('qq', dict(state='login_required', source_ref='s'))
        self.assertEqual(value['state'], 'needs_setup')

    def test_disconnected_maps_to_offline(self):
        value = world.channel_fact('qq', dict(state='disconnected', source_ref='s'))
        self.assertEqual(value['state'], 'offline')

    def test_unknown_word_is_unknown_not_guessed(self):
        value = world.channel_fact('qq', dict(state='some_new_state', source_ref='s'))
        self.assertEqual(value['state'], 'unknown')

    def test_detail_says_it_is_a_channel_not_an_action(self):
        """她读到的是一行字，看不到 subject 字段。"""
        for state in ('connected', 'disconnected', 'disabled', 'login_required', 'unknown'):
            value = world.channel_fact('qq', dict(state=state, source_ref='s'))
            self.assertIn('通道', value['detail'], 'state=%s 的 detail 没说明这是通道' % state)
            self.assertIn('不是某个动作', value['detail'])

    def test_original_account_state_is_preserved_in_raw(self):
        value = world.channel_fact('qq', dict(state='login_required', source_ref='s'))
        self.assertEqual(value['account_state'], 'login_required',
                         '收敛词表不等于丢掉原始说法')


class TownFactTests(unittest.TestCase):
    """小镇：读配置判断「有没有配」，读 state 判断「连没连上」。"""

    @staticmethod
    def _bridge(enabled=True, code='fbo_test', state='online', error=''):
        return SimpleNamespace(config=SimpleNamespace(enabled=enabled, code=code),
                               state=state, _error=error)

    def test_online_town_is_ready(self):
        """今晚的核心 bug：小镇跑着却报 needs_setup。"""
        value = world.town_fact(self._bridge(state='online'))
        self.assertEqual(value['state'], 'ready')
        self.assertIn('连着', value['detail'])

    def test_disabled_is_needs_setup(self):
        value = world.town_fact(self._bridge(enabled=False, state='disabled'))
        self.assertEqual(value['state'], 'needs_setup')
        self.assertIn('没有启用', value['detail'])

    def test_missing_code_is_needs_setup_about_code(self):
        value = world.town_fact(self._bridge(code='', state='disabled'))
        self.assertEqual(value['state'], 'needs_setup')
        self.assertIn('接入码', value['detail'])

    def test_reconnecting_is_offline_and_carries_error(self):
        value = world.town_fact(self._bridge(state='reconnecting', error='TimeoutError'))
        self.assertEqual(value['state'], 'offline')
        self.assertIn('TimeoutError', value['detail'])

    def test_unseen_state_is_unknown(self):
        value = world.town_fact(self._bridge(state='brand_new'))
        self.assertEqual(value['state'], 'unknown')

    def test_missing_bridge_is_unknown(self):
        self.assertEqual(world.town_fact(None)['state'], 'unknown')

    def test_raising_bridge_is_unknown(self):
        class Boom:
            config = SimpleNamespace(enabled=True, code='fbo_x')

            @property
            def state(self):
                raise RuntimeError('boom')
        self.assertEqual(world.town_fact(Boom())['state'], 'unknown')


class TownNotStartedTests(unittest.TestCase):
    """`state='disabled'` 有两种含义，必须靠配置分开。

    `life/companion_town.py:172` 构造时就把 state 设成 'disabled'，
    只有 start() → set_active() → connecting → _bind() 成功后才变 online。
    """

    def test_enabled_config_but_disabled_state_is_unknown_not_needs_setup(self):
        bridge = SimpleNamespace(config=SimpleNamespace(enabled=True, code='fbo_x'),
                                 state='disabled', _error='')
        value = world.town_fact(bridge)
        self.assertEqual(value['state'], 'unknown',
                         '配置是好的、桥还没起，这是「不知道」不是「没启用」')
        self.assertIn('还没开始', value['detail'])

    def test_disabled_config_is_still_needs_setup(self):
        bridge = SimpleNamespace(config=SimpleNamespace(enabled=False, code=''),
                                 state='disabled', _error='')
        self.assertEqual(world.town_fact(bridge)['state'], 'needs_setup')

    def test_paused_is_offline(self):
        bridge = SimpleNamespace(config=SimpleNamespace(enabled=True, code='fbo_x'),
                                 state='paused', _error='')
        self.assertEqual(world.town_fact(bridge)['state'], 'offline')


class BilibiliFactTests(unittest.TestCase):
    def test_plugin_missing_is_needs_setup_about_plugin(self):
        value = world.bilibili_fact(dict(available=False, credential_present=False,
                                         connected=False))
        self.assertEqual(value['state'], 'needs_setup')
        self.assertIn('插件', value['detail'])

    def test_no_credential_is_needs_setup_about_login(self):
        value = world.bilibili_fact(dict(available=True, credential_present=False,
                                         connected=False))
        self.assertEqual(value['state'], 'needs_setup')
        self.assertIn('登录', value['detail'])
        self.assertNotIn('插件没有安装', value['detail'])

    def test_process_down_is_offline(self):
        value = world.bilibili_fact(dict(available=True, credential_present=True,
                                         connected=False))
        self.assertEqual(value['state'], 'offline')

    def test_all_good_is_ready(self):
        value = world.bilibili_fact(dict(available=True, credential_present=True,
                                         connected=True))
        self.assertEqual(value['state'], 'ready')

    def test_three_causes_give_three_different_details(self):
        details = {world.bilibili_fact(dict(available=a, credential_present=c, connected=k))['detail']
                   for a, c, k in ((False, False, False), (True, False, False), (True, True, False))}
        self.assertEqual(len(details), 3, '三种原因必须说成三句不同的话')


class SelfProcessIsDeliberatelyExcludedTests(unittest.TestCase):
    """self_process 是环境描述，不是可用性状态——**故意不接**。"""

    def test_adapter_refuses_instead_of_guessing_a_state(self):
        with self.assertRaises(NotImplementedError):
            world.self_process_fact(dict(can_write_own_source=True))

    def test_collect_never_returns_self_process(self):
        core = SimpleNamespace(executor=SimpleNamespace(phone_context=lambda: dict(devices=[])),
                               town=SimpleNamespace(state='online', _error=''))
        refs = [f['fact_ref'] for f in world.collect(core)]
        self.assertNotIn('self.process', refs, '环境事实不该混进可用性事实')


class CollectTests(unittest.TestCase):
    def test_one_broken_source_does_not_break_the_rest(self):
        class Boom:
            def __getattr__(self, name):
                raise RuntimeError('boom')
        core = SimpleNamespace(executor=Boom(), town=None)
        facts = world.collect(core)
        self.assertTrue(facts, '一个源炸了不该让整组为空')
        self.assertTrue(all(f['state'] in world.STATES for f in facts))

    def test_collect_covers_five_sources(self):
        """phone + town + bilibili + channel.qq + channel.wechat。"""
        core = SimpleNamespace(
            cfg=None,
            executor=SimpleNamespace(phone_context=lambda: dict(devices=[])),
            town=SimpleNamespace(state='online', _error=''))
        import life.companion_awareness as aw
        original = aw._channel_facts
        aw._channel_facts = lambda c, cfg: dict(
            qq=dict(state='disabled', source_ref='q'), wechat=dict(state='disabled', source_ref='w'))
        try:
            refs = [f['fact_ref'] for f in world.collect(core)]
        finally:
            aw._channel_facts = original
        for expected in ('phone.link', 'town.link', 'bilibili.status',
                         'channel.qq', 'channel.wechat'):
            self.assertIn(expected, refs, 'collect 少了一个源：%s' % expected)

    def test_channel_read_failure_degrades_to_unknown_not_fatal(self):
        core = SimpleNamespace(cfg=None, executor=None, town=None)
        import life.companion_awareness as aw
        original = aw._channel_facts

        def boom(c, cfg):
            raise RuntimeError('channel read failed')
        aw._channel_facts = boom
        try:
            facts = world.collect(core)
        finally:
            aw._channel_facts = original
        channels = [f for f in facts if f['fact_ref'].startswith('channel')]
        self.assertTrue(channels, '通道整体读不到也要留一条 unknown')
        self.assertTrue(all(f['state'] == 'unknown' for f in channels))

    def test_collect_returns_only_declared_states(self):
        core = SimpleNamespace(executor=SimpleNamespace(phone_context=lambda: dict(devices=[])),
                               town=SimpleNamespace(state='online', _error=''))
        for value in world.collect(core):
            self.assertIn(value['state'], world.STATES)


if __name__ == '__main__':
    unittest.main()


class CapabilityCatalogueNotDegradedTests(unittest.TestCase):
    """第 2 步的硬要求：改完 `_channel_facts` 相关逻辑，能力目录不能退化。

    判据（captain 定的）：`channel.qq` 仍然给出 offline + usable=false + 原因。
    这里用真实 `_channel_facts` 跑一遍 `capabilities()`，只在通道读得到的条件下断言。
    """

    def _catalogue(self, channels):
        import life.companion_awareness as aw
        original = aw._channel_facts
        aw._channel_facts = lambda core, cfg: channels
        try:
            core = SimpleNamespace(cfg=None, executor=None, town=None, home='/tmp')
            return {item['capability_ref']: item
                    for item in aw.capabilities(core, state=dict(available_actions=[]))
                    if str(item.get('capability_ref', '')).startswith('channel.')}
        finally:
            aw._channel_facts = original

    def test_disabled_channel_is_needs_setup_not_offline(self):
        """2026-11-18 行为变更：主动关闭判成 needs_setup，不再判 offline。

        旧代码判 offline（=掉线），而 reason 文案写着「不是掉线，是主动关的」——
        判定和措辞用的不是同一份事实。现在两者都来自事实层，这个矛盾消失。
        """
        rows = self._catalogue(dict(
            qq=dict(state='disabled', enabled=False, source_ref='q', source_scope='channel_config'),
            wechat=dict(state='disabled', enabled=False, source_ref='w', source_scope='channel_config')))
        qq = rows['channel.qq']
        self.assertEqual(qq['status'], 'needs_setup',
                         '主动关闭应该判 needs_setup（需要人去开），不是 offline（掉线）')
        self.assertFalse(qq['usable'])
        self.assertTrue(qq['reason'], '原因不能丢')
        self.assertEqual(qq['state_scope'], 'disabled', 'state_scope 必须保持原义')
        self.assertIsNone(qq['dispatchable'], '通道不是动作 kind，dispatchable 必须是 None')

    def test_connected_channel_is_ready_and_usable(self):
        rows = self._catalogue(dict(
            qq=dict(state='connected', enabled=True, source_ref='q', source_scope='runtime_snapshot'),
            wechat=dict(state='connected', enabled=True, source_ref='w', source_scope='runtime_snapshot')))
        qq = rows['channel.qq']
        self.assertEqual(qq['status'], 'ready')
        self.assertTrue(qq['usable'])

    def test_login_required_is_needs_setup(self):
        rows = self._catalogue(dict(
            qq=dict(state='login_required', enabled=True, source_ref='q', source_scope='channel_config'),
            wechat=dict(state='login_required', enabled=True, source_ref='w', source_scope='channel_config')))
        self.assertEqual(rows['channel.qq']['status'], 'needs_setup')

    def test_world_facts_do_not_change_catalogue(self):
        """事实层只是读，不能反过来影响能力目录。"""
        channels = dict(
            qq=dict(state='disabled', enabled=False, source_ref='q', source_scope='channel_config'),
            wechat=dict(state='disabled', enabled=False, source_ref='w', source_scope='channel_config'))
        before = self._catalogue(channels)
        core = SimpleNamespace(cfg=None, executor=None, town=None, home='/tmp')
        world.channel_facts(core)
        after = self._catalogue(channels)

        # observed_at 是墙上时间，两次调用跨秒就会不同——那不是退化。
        # 比较除时间戳外的全部字段。
        def strip_time(rows):
            return {ref: {k: v for k, v in item.items() if k != 'checked_at'}
                    for ref, item in rows.items()}
        self.assertEqual(strip_time(before), strip_time(after), '读了事实层之后能力目录变了')


class TownClosedTests(unittest.TestCase):
    """`closed` 是 bridge 已被 close()，读不到有效状态——按 F-2 说 unknown。"""

    def test_closed_is_unknown_not_needs_setup(self):
        value = world.town_fact(SimpleNamespace(config=SimpleNamespace(enabled=True, code='fbo_x'),
                                                 state='closed', _error=''))
        self.assertEqual(value['state'], 'unknown',
                         'closed 说成 needs_setup 会暗示"你去开一下"，但用户什么都做不了')

    def test_closed_detail_does_not_blame_configuration(self):
        value = world.town_fact(SimpleNamespace(config=SimpleNamespace(enabled=True, code='fbo_x'),
                                                 state='closed', _error=''))
        self.assertIn('关闭', value['detail'])
        self.assertIn('不是配置问题', value['detail'])


class DshThreeStateTests(unittest.TestCase):
    """dsh 三态必须分开：None（读不到）≠ False（明确没有）。"""

    def _exec(self, paths):
        return SimpleNamespace(_dsh_paths=paths, original=None)

    def test_available_is_ready(self):
        value = world.dsh_fact(self._exec(lambda: ('/r', '/n', '/e')))
        self.assertEqual(value['state'], 'ready')

    def test_probed_absent_is_needs_setup(self):
        value = world.dsh_fact(self._exec(lambda: None))
        self.assertEqual(value['state'], 'needs_setup')

    def test_probe_raising_is_unknown_not_needs_setup(self):
        def boom():
            raise RuntimeError('io')
        value = world.dsh_fact(self._exec(boom))
        self.assertEqual(value['state'], 'unknown',
                         '探测抛错不能说成"本机没有运行时"——那正是 F-2 那个 bug')

    def test_no_holder_in_chain_is_unknown(self):
        self.assertEqual(world.dsh_fact(SimpleNamespace(original=None))['state'], 'unknown')

    def test_collect_includes_dsh(self):
        core = SimpleNamespace(
            cfg=None,
            executor=SimpleNamespace(_dsh_paths=lambda: ('/r', '/n', '/e'), original=None,
                                     phone_context=lambda: dict(devices=[])),
            town=SimpleNamespace(state='online', _error=''))
        import life.companion_awareness as aw
        original = aw._channel_facts
        aw._channel_facts = lambda c, cfg: dict(qq=dict(state='disabled', source_ref='q'))
        try:
            refs = [f['fact_ref'] for f in world.collect(core)]
        finally:
            aw._channel_facts = original
        self.assertIn('dsh.runtime', refs)


class DshCopyStaysInSyncTests(unittest.TestCase):
    """走链逻辑有两份拷贝（事实层 / awareness），必须行为一致。

    captain 要求：复制代码至少要留指针，并且行为要对得上。
    """

    def _cases(self):
        return [
            ('direct paths', SimpleNamespace(_dsh_paths=lambda: ('r', 'n', 'e'), original=None)),
            ('absent', SimpleNamespace(_dsh_paths=lambda: None, original=None)),
            ('raises', SimpleNamespace(_dsh_paths=lambda: (_ for _ in ()).throw(RuntimeError('x')),
                                       original=None)),
            ('no holder', SimpleNamespace(original=None)),
            ('one hop', SimpleNamespace(original=SimpleNamespace(
                _dsh_paths=lambda: ('r', 'n', 'e'), original=None))),
            ('deep beyond limit', self._chain(6)),
        ]

    @staticmethod
    def _chain(depth):
        node = SimpleNamespace(original=None)
        for _ in range(depth):
            node = SimpleNamespace(original=node)
        return node

    def test_both_implementations_agree(self):
        from life import companion_awareness as aw
        for label, executor in self._cases():
            with self.subTest(case=label):
                mine = world._dsh_endpoint_probe(executor)
                theirs = aw._dsh_endpoint(executor)
                self.assertEqual(mine, theirs,
                                 '%s：事实层与 awareness 的走链结果不一致' % label)

    def test_pointer_comments_exist_on_both_sides(self):
        """两边都必须说明对方存在，否则一年后没人知道有两份。"""
        import io
        world_src = io.open(world.__file__, encoding='utf-8').read()
        aw_src = io.open(world.__file__.replace('companion_world.py', 'companion_awareness.py'),
                         encoding='utf-8').read()
        self.assertIn('_dsh_endpoint', world_src)
        self.assertIn('companion_world', aw_src, 'awareness 那边没留指回事实层的注释')


class BodyFactThreeStateTests(unittest.TestCase):
    """`_body_context` 初始值是 dict(connected=False)，与「连过又断了」同形。

    光凭 `connected` 分不开两者——必须看有没有「桥推过状态」的痕迹。
    """

    def test_never_connected_is_unknown_not_offline(self):
        value = world.body_fact(dict(connected=False))
        self.assertEqual(value['state'], 'unknown',
                         '从没连过就说 offline 是「不知道」说成「知道」')

    def test_disconnected_after_connecting_is_offline(self):
        value = world.body_fact(dict(connected=False, figure='cat', position=dict(x=0.5)))
        self.assertEqual(value['state'], 'offline')
        self.assertIn('连过但现在没连上', value['detail'])

    def test_single_trace_field_is_enough(self):
        self.assertEqual(world.body_fact(dict(connected=False, can_walk=True))['state'], 'offline')

    def test_empty_trace_values_do_not_count(self):
        """`figure=''` 这种空值不算「连过」的痕迹。"""
        value = world.body_fact(dict(connected=False, figure='', position={}, can_walk=None))
        self.assertEqual(value['state'], 'unknown')

    def test_connected_is_ready(self):
        value = world.body_fact(dict(connected=True, figure='cat', can_walk=True))
        self.assertEqual(value['state'], 'ready')

    def test_connected_but_cannot_walk_is_still_ready(self):
        """能连上就是 ready；不能走动是能力受限，不是链路故障。"""
        value = world.body_fact(dict(connected=True, figure='cat', can_walk=False))
        self.assertEqual(value['state'], 'ready')
        self.assertIn('不能走动', value['detail'])


class ComputerVsBodyAreIndependentTests(unittest.TestCase):
    """「能看屏幕」和「小人能动」是两条链路，不能互相顶替。"""

    def test_computer_up_but_body_down(self):
        self.assertEqual(world.computer_fact(object())['state'], 'ready')
        self.assertEqual(world.body_fact(dict(connected=False, figure='cat'))['state'], 'offline')

    def test_both_down_give_different_details(self):
        computer = world.computer_fact(None)
        body = world.body_fact(dict(connected=False, figure='cat'))
        self.assertEqual(computer['state'], 'offline')
        self.assertEqual(body['state'], 'offline')
        self.assertNotEqual(computer['detail'], body['detail'],
                            '两条链路的说明不能是同一句')
        self.assertIn('屏幕', computer['detail'])
        self.assertIn('身体', body['detail'])



class TownKindGateTests(unittest.TestCase):
    """`_messages()` 里 town 进不进 `kinds`，要看**运行状态**，不能只看配置。

    2026-11-18 实测：配置好但桥没起时，town 进了 `kinds` → 能力目录说
    「能用（去小镇和角色互动）」，而 `town_fact()` 说「不能确认」。
    两处不一致，而且是最危险的方向——她以为能用，用户去试才失败。
    """

    # 与 life/companion.py `_messages` 里的门槛保持同一份语义。
    GATE = ('connecting', 'online', 'paused', 'reconnecting')

    def _kind_included(self, enabled, code, state):
        config = SimpleNamespace(enabled=enabled, code=code)
        town = SimpleNamespace(config=config, state=state)
        return bool(town.config.enabled and town.config.code and town.state in self.GATE)

    def test_unstarted_bridge_does_not_admit_town(self):
        self.assertFalse(self._kind_included(True, 'fbo_x', 'disabled'),
                         '配置好但桥没 start，不该让 town 进 kinds')

    def test_online_bridge_admits_town(self):
        self.assertTrue(self._kind_included(True, 'fbo_x', 'online'))

    def test_connecting_still_admits(self):
        """正在连也算「它会连上」，可以让它出现在清单里。"""
        self.assertTrue(self._kind_included(True, 'fbo_x', 'connecting'))

    def test_missing_config_does_not_admit(self):
        self.assertFalse(self._kind_included(False, 'fbo_x', 'online'))
        self.assertFalse(self._kind_included(True, '', 'online'))

    def test_closed_bridge_does_not_admit(self):
        self.assertFalse(self._kind_included(True, 'fbo_x', 'closed'))

    def test_gate_matches_the_real_source(self):
        """门槛取值必须和 life/companion.py 里那份一致，不能各写各的。"""
        import io as _io
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1] / 'life' / 'companion.py').read_text('utf-8')
        for state in self.GATE:
            self.assertIn("'%s'" % state, src,
                          'companion.py 里没有状态词 %s，两边可能走样了' % state)



class TownStartFailureIsolatedTests(unittest.TestCase):
    """小镇桥是附属功能：它起不来不能拖垮整个 `start()`（B1，2026-11-18）。

    以前 `await self.town.start()` 没有保护，抛错会冒到 `start()`；而 `_started=True`
    已在它之前设好，重试会被 `start()` 开头挡掉——桥永远起不来且无日志。
    """

    def test_source_wraps_town_start_in_try(self):
        """结构断言：那一处 `await self.town.start()` 必须在 try 里。"""
        import io as _io
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1] / 'life' / 'companion.py').read_text('utf-8')
        index = src.index('        await self.bilibili_social.start()')
        window = src[:index][-900:]
        self.assertIn('try:', window, 'town.start() 外面没有 try')
        self.assertIn('except Exception', window, '没有兜住异常')
        self.assertIn('小镇桥启动失败', window, '失败时要有可搜索的日志文案')

    def test_failure_is_audited_not_only_logged(self):
        """captain 要求：失败要写进审计，否则日志滚动后就找不到了。"""
        import io as _io
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1] / 'life' / 'companion.py').read_text('utf-8')
        index = src.index('        await self.bilibili_social.start()')
        window = src[:index][-900:]
        self.assertIn("self._audit('error'", window, '失败没有写进 companion_activity')
        self.assertIn("task='town'", window, '审计里要标明是哪个组件')

    def test_audit_failure_does_not_break_startup(self):
        """审计自己失败也不能中断启动。"""
        import io as _io
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1] / 'life' / 'companion.py').read_text('utf-8')
        index = src.index('        await self.bilibili_social.start()')
        window = src[:index][-900:]
        self.assertGreaterEqual(window.count('except Exception'), 2,
                                '审计那层也要兜住')
