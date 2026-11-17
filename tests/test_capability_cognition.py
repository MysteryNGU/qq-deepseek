# -*- coding: utf-8 -*-
"""能力自知的回归测试：靶点 A/B/C/E（docs/capability-chain-audit.md §7）。

全部离线，不连模型、不写生产库：

* ``_settle`` 用真 ``TaskService`` + 真 ``repo``，验证 ``completed`` 真的写回
  ``last_receipt``（靶点 A：审计实测前 ``step_done`` 恒 False）；
* ``capabilities()`` 用 ``__new__`` 壳对象，验证掉线的 kind 留条目带原因（靶点 C）、
  ``channel.*`` 不再由 ``available_actions`` 推断（靶点 B）；
* ``_action_fields`` 按 op 过滤（靶点 E：``inspect`` 带 ``goal`` 不再 TypeError）。

跑法::

    python -B -X utf8 -m unittest tests.test_capability_cognition -v
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class _Exec:
    """壳执行器：只回答能力探测会问到的两件事。"""

    def __init__(self, phone=None, dsh=False):
        self._phone = phone
        self._dsh = dsh

    def phone_context(self):
        return self._phone if self._phone is not None else dict(connected=False, devices=[])

    def _dsh_paths(self):
        return ('/stub', '/stub/node', 'lib/bin.js') if self._dsh else None


def _core(executor, *, home='.'):
    return SimpleNamespace(
        executor=executor, repo=None, agents=None, home=home,
        cfg=SimpleNamespace(),
        life=SimpleNamespace(agent_id='agent-test'),
        spending=SimpleNamespace(policy=SimpleNamespace(enabled=False)),
    )


# ---------------------------------------------------------------- 靶点 C 掉线留条目

class OfflineCapabilities(unittest.TestCase):
    """能力掉线不许静默消失：必须留一条带具体原因的条目。"""

    def setUp(self):
        from life import companion_awareness as awareness
        self.awareness = awareness

    def _entries(self, available, *, executor=None):
        core = _core(executor or _Exec())
        catalogue = self.awareness.capabilities(core, dict(available_actions=available))
        return {item['capability_ref']: item for item in catalogue}

    def test_phone_disconnected_is_offline_with_reason_not_missing(self):
        entries = self._entries(['web', 'command', 'skill', 'work', 'memory', 'growth'])
        self.assertIn('action.phone', entries, '手机掉线时条目不能整个消失')
        item = entries['action.phone']
        self.assertEqual(item['status'], 'offline')
        self.assertTrue(item['reason'], 'offline 必须带原因，否则她说不出为什么')
        self.assertIn('手机', item['reason'])
        self.assertEqual(item['checked_at'][:4], time.strftime('%Y'), '状态必须带观察时刻')
        self.assertIn('available_actions:absent', item['evidence'])

    def test_phone_with_no_registration_explains_what_to_do(self):
        entries = self._entries(['command'])
        reason = entries['action.phone']['reason']
        self.assertIn('90', reason, '原因要说出判定依据（90 秒注册窗口）')
        self.assertIn('前台', reason, '要给出恢复办法，不能只说「不可用」')

    def test_phone_registered_without_stream_says_dispatch_would_fail(self):
        phone = dict(connected=True, devices=[dict(device_id='d1', has_stream=False)])
        entries = self._entries(['command'], executor=_Exec(phone=phone))
        reason = entries['action.phone']['reason']
        self.assertIn('前台', reason)
        self.assertNotIn('没有上报', reason, '注册过就不能说成没上报')

    def test_desktop_and_town_carry_their_own_reasons(self):
        entries = self._entries(['command'])
        self.assertEqual(entries['action.computer']['status'], 'offline')
        self.assertIn('桌面', entries['action.computer']['reason'])
        self.assertEqual(entries['action.town']['status'], 'needs_setup')
        self.assertIn('接入码', entries['action.town']['reason'])

    def test_offline_entries_keep_their_usage_hint(self):
        """掉线也要能说出「这个东西怎么用」，否则她从零开始。"""
        entries = self._entries(['command'])
        self.assertTrue(entries['action.phone'].get('what'), '掉线条目也要带用法要点')
        self.assertEqual(entries['action.phone']['how_ref'], 'companion_context.GUIDES[phone]')

    def test_available_kind_is_ready_and_never_duplicated(self):
        entries = self._entries(['web', 'phone', 'command'])
        self.assertEqual(entries['action.phone']['status'], 'ready')
        core = _core(_Exec(phone=dict(connected=True, devices=[dict(device_id='d', has_stream=True)])))
        refs = [item['capability_ref'] for item in self.awareness.capabilities(core, dict(available_actions=['phone']))]
        self.assertEqual(refs.count('action.phone'), 1, '可用的 kind 只能有一条条目')

    def test_offline_reasons_used_for_prompt_matches_catalogue(self):
        """请求体里那句话和 memory op=capabilities 读到的必须是同一句。"""
        core = _core(_Exec())
        rows = {row['kind']: row for row in self.awareness.offline_reasons(core, ['command'])}
        self.assertIn('phone', rows)
        entries = self._entries(['command'])
        self.assertEqual(rows['phone']['reason'], entries['action.phone']['reason'])


# ---------------------------------------------------------------- 靶点 B 通道读真实登录

class ChannelStatus(unittest.TestCase):
    """channel.* 的 ready 只能来自通道自己的登录状态。"""

    def setUp(self):
        from life import companion_awareness as awareness
        self.awareness = awareness
        self.temp = tempfile.TemporaryDirectory(prefix='chan-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)

    def _entries(self, available, *, qq=None):
        core = _core(_Exec(), home=self.home)
        if qq is not None:
            core._channel_facts_override = qq
        with_override = self.awareness._channel_facts
        self.awareness._channel_facts = lambda c, cfg: (qq if qq is not None else with_override(c, cfg))
        self.addCleanup(setattr, self.awareness, '_channel_facts', with_override)
        catalogue = self.awareness.capabilities(core, dict(available_actions=available))
        return {item['capability_ref']: item for item in catalogue}

    def test_qq_not_logged_in_is_not_ready_even_when_actions_reported(self):
        """审计的原缺陷：报了动作就两个通道都 ready。"""
        entries = self._entries(['web', 'command'])
        self.assertNotEqual(entries['channel.qq']['status'], 'ready',
                            'QQ 没登录时 channel.qq 不能是 ready')
        self.assertTrue(entries['channel.qq']['reason'])
        self.assertNotEqual(entries['channel.wechat']['status'], 'ready')

    def test_disabled_channel_says_it_was_turned_off(self):
        facts = dict(qq=dict(state='disabled', enabled=False, observed_at='2026-11-17T23:00:00+08:00',
                             source_ref='life/companion_qq.py#load_qq_settings',
                             source_scope='channel_config', value=None, error=None))
        entries = self._entries(['command'], qq=facts)
        item = entries['channel.qq']
        self.assertNotEqual(item['status'], 'ready')
        self.assertIn('关', item['reason'], '关掉要说成主动关的，不是掉线')
        self.assertEqual(item['state_scope'], 'disabled')
        self.assertEqual(item['checked_at'], '2026-11-17T23:00:00+08:00')

    def test_connected_channel_is_ready_with_source(self):
        facts = dict(qq=dict(state='connected', enabled=True, observed_at='2026-11-17T23:00:00+08:00',
                             source_ref='life/companion_im.py#ImManager.snapshot',
                             source_scope='runtime_snapshot', value='10001', error=None))
        entries = self._entries(['command'], qq=facts)
        self.assertEqual(entries['channel.qq']['status'], 'ready')
        self.assertIsNone(entries['channel.qq']['reason'])
        self.assertEqual(entries['channel.qq']['source_scope'], 'runtime_snapshot')

    def test_unconfigured_channel_is_not_ready(self):
        entries = self._entries(['command'])
        self.assertNotEqual(entries['channel.wechat']['status'], 'ready')


# ---------------------------------------------------------------- 三层摘要格式

class SummaryShape(unittest.TestCase):
    def test_summary_has_three_separated_layers(self):
        from life import companion_awareness as awareness
        catalogue = awareness.capabilities(_core(_Exec()), dict(available_actions=['web', 'command']))
        summary = awareness.capability_summary(catalogue)
        for key in ('usable', 'unusable', 'how'):
            self.assertIn(key, summary, '三层认知必须分开：' + key)
        self.assertTrue(any(row.startswith('command') for row in summary['usable']))
        self.assertTrue(any(row.startswith('phone') for row in summary['unusable']),
                        '手机掉线必须出现在「当前不可用」里：' + str(summary['unusable']))
        self.assertIn('op=capabilities', summary['how'], '要告诉她细节去哪个动作读')

    def test_summary_keeps_legacy_keys(self):
        """ready/not_ready 是旧形状：下游按它们判空与计数，现在给数量而不是重列名单。"""
        from life import companion_awareness as awareness
        summary = awareness.capability_summary(
            awareness.capabilities(_core(_Exec()), dict(available_actions=['command'])))
        for key in ('total', 'counts', 'ready', 'not_ready', 'note'):
            self.assertIn(key, summary)
        self.assertEqual(summary['ready'], len(summary['usable']), 'ready 必须与 usable 条数一致')
        self.assertGreater(summary['not_ready'], 0, 'not_ready 必须能被判空')
        self.assertIn('command', summary['usable'][0])


# ---------------------------------------------------------------- 靶点 E agent 字段按 op 过滤

class AgentFieldFilter(unittest.TestCase):
    def test_inspect_with_goal_no_longer_reaches_the_signature(self):
        """审计实测：goal 会一路展开成 inspect(goal=...)，变成 TypeError。"""
        from life.companion_agents import _action_fields
        fields = _action_fields(dict(op='inspect', agent_id='companion-agent_x', goal='看看进度'))
        self.assertNotIn('goal', fields, 'inspect 不认识 goal，必须在入口丢掉')
        self.assertEqual(fields['agent_id'], 'companion-agent_x')

    def test_create_with_kind_does_not_leak_into_create(self):
        from life.companion_agents import _action_fields
        fields = _action_fields(dict(op='create', goal='写一份清单', kind='agent', project='p1'))
        self.assertNotIn('kind', fields)
        self.assertEqual(fields['goal'], '写一份清单')

    def test_create_keeps_its_own_fields(self):
        from life.companion_agents import _action_fields
        fields = _action_fields(dict(op='create', goal='x', node='work', deliverables=['a.md'],
                                     acceptance=['文件存在'], project='p'))
        for key in ('goal', 'node', 'deliverables', 'acceptance'):
            self.assertIn(key, fields)

    def test_wait_and_read_artifact_keep_their_fields(self):
        from life.companion_agents import _action_fields
        wait = _action_fields(dict(op='wait', agent_id='a', wait_s=0, goal='x'))
        self.assertEqual(wait.get('wait_s'), 0)
        self.assertNotIn('goal', wait)
        read = _action_fields(dict(op='read_artifact', agent_id='a', artifact_id='art1', offset=0))
        self.assertEqual(read['artifact_id'], 'art1')

    def test_required_arguments_still_refused_with_clear_code(self):
        from life.companion_agents import AgentError, _action_fields
        with self.assertRaises(AgentError) as caught:
            _action_fields(dict(op='send', agent_id='a'))
        self.assertEqual(caught.exception.code, 'text_required')

    def test_op_fields_cover_every_op(self):
        from life.companion_agents import OP_FIELDS, OPS
        self.assertEqual(set(OP_FIELDS), set(OPS), '每个 op 都要有参数白名单')


# ---------------------------------------------------------------- 靶点 A 回执写回项目

class SettleWritesReceipt(unittest.TestCase):
    """``_settle`` 认 completed：这是「结果回查」环节最关键的一处。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='settle-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home / 'data').mkdir(parents=True, exist_ok=True)

    def _core(self):
        from life.companion import Companion
        from tests.test_agent_repair import FakeModel, _cfg
        core = Companion(_cfg(self.home), self.home, model=FakeModel())
        self.addCleanup(core.life.close)
        return core

    def _project_and_row(self, core):
        from life.text import stable_id
        project = core.tasks.ensure(core.scope, '回执测试', None)
        source, _ = core.life.ingest(core.scope, 'message', 'user_report', '查一下磁盘',
                                     payload=dict(input_source='qq:private:10001'))
        row_id = stable_id('companion-action', 'test-' + str(time.time()))
        with core.repo.tx():
            core.repo._exec('INSERT INTO companion_actions (id,agent,source,project_ref,action,status,created,updated) '
                            'VALUES (?,?,?,?,?,?,?,?)',
                            (row_id, core.life.agent_id, source.id, '回执测试',
                             json.dumps(dict(kind='command', command='df -h /', project='回执测试',
                                             goal='查磁盘'), ensure_ascii=False),
                             'running', time.time(), time.time()))
        row = core.repo._one('SELECT * FROM companion_actions WHERE id=?', (row_id,))
        return '回执测试', row

    def test_completed_status_writes_last_receipt(self):
        core = self._core()
        project_ref, row = self._project_and_row(core)
        core._settle(row, dict(status='completed', output='可用 12G', return_code=0))
        saved = core.tasks.get(project_ref, core.scope.scope_id) or {}
        receipt = saved.get('last_receipt') or {}
        self.assertEqual(receipt.get('action_id'), row['id'],
                         'completed 回执必须写回项目，否则续行会重读同一份东西')
        self.assertIn('12G', receipt.get('output') or '')
        # 下游判据：step_done 现在为真。
        step_done = bool(isinstance(receipt.get('at'), (int, float))
                         and receipt['at'] >= (saved.get('next_step_set_at') or 0))
        self.assertTrue(step_done, 'step_done 必须为真（审计实测改之前恒为 False）')

    def test_succeeded_still_accepted_for_compatibility(self):
        core = self._core()
        project_ref, row = self._project_and_row(core)
        core._settle(row, dict(status='succeeded', output='旧写法', return_code=0))
        saved = core.tasks.get(project_ref, core.scope.scope_id) or {}
        self.assertEqual((saved.get('last_receipt') or {}).get('action_id'), row['id'])

    def test_failed_status_does_not_write_receipt(self):
        """失败不是进度：写了就等于宣布这步做完了。"""
        core = self._core()
        project_ref, row = self._project_and_row(core)
        core._settle(row, dict(status='failed', output='not found', return_code=127))
        saved = core.tasks.get(project_ref, core.scope.scope_id) or {}
        self.assertFalse(saved.get('last_receipt'), 'failed 不能写 last_receipt')

    def test_settled_ok_vocabulary_matches_executors(self):
        from life.companion import SETTLED_OK
        self.assertIn('completed', SETTLED_OK)
        self.assertIn('succeeded', SETTLED_OK)
        for bad in ('failed', 'cancelled', 'interrupted', 'queued'):
            self.assertNotIn(bad, SETTLED_OK)


# ---------------------------------------------------------------- 提示词接线

class PromptWiring(unittest.TestCase):
    def test_offline_hint_reaches_tool_instructions(self):
        from life.companion_context import tool_instructions_with_offline
        text = tool_instructions_with_offline(['command'], [dict(kind='phone', reason='手机没有活动连接')])
        self.assertIn('command在execution', text, '可用能力的说明照常注入')
        self.assertIn('手机没有活动连接', text, '掉线的原因必须出现在请求体里')

    def test_offline_hint_accepts_tuples(self):
        """拼接点两边各写各的形状：2026-11-17 生产每轮 AttributeError 就是这里。

        纯函数单测全绿、真实 _messages 全崩，因为没人测过「调用方实际传什么形状」。
        这条按调用方当时真实的入参形状钉住，两种都得收。
        """
        from life.companion_context import tool_instructions_with_offline
        text = tool_instructions_with_offline(['command'], [('phone', '没连上')])
        self.assertIn('phone：没连上', text, '元组入参必须能读，不能 AttributeError')

    def test_offline_hint_tolerates_junk_entries(self):
        from life.companion_context import tool_instructions_with_offline
        text = tool_instructions_with_offline(['command'], [None, 'x', ('phone',), 42,
                                                            dict(kind='computer', reason='没连')])
        self.assertIn('computer：没连', text, '认不出的条目跳过，认得出的照常注入')

    def test_offline_hint_matches_shape_of_offline_reasons(self):
        """真实生产形状：offline_reasons() 返回 dict 列表，直接喂进去必须能用。"""
        from life import companion_awareness as awareness
        from life.companion_context import tool_instructions_with_offline
        rows = awareness.offline_reasons(_core(_Exec()), ['command'])
        self.assertTrue(rows, '壳执行器下应有 phone/computer/body 掉线')
        text = tool_instructions_with_offline(['command'], rows)
        for row in rows:
            if row['kind'] in ('phone', 'computer', 'body'):
                self.assertIn(row['kind'] + '：', text)

    def test_offline_hint_skips_kinds_nobody_needs_explained(self):
        from life.companion_context import tool_instructions_with_offline
        text = tool_instructions_with_offline(['command'], [dict(kind='town', reason='小镇没启用')])
        self.assertNotIn('小镇没启用', text, '只有连接类掉线才值得每轮占一行')

    def test_plain_tool_instructions_unchanged(self):
        from life.companion_context import tool_instructions
        self.assertEqual(tool_instructions(['command']), tool_instructions(['command']).strip())
        self.assertNotIn('现在用不了的', tool_instructions(['command']))

    def test_agent_capability_says_arguments_not_top_level_fields(self):
        from life import companion_awareness as awareness
        item = awareness.agent_capability(_core(_Exec()), mode='off', endpoint=False)
        self.assertIn('arguments', item['what'],
                      'agent 的说明必须写明参数放 arguments，否则会照顶层字段去发')


# ---------------------------------------------------------------- guard.watchdog

class GuardWatchdog(unittest.TestCase):
    """基线里她说「没有人替我兜底」，而 watchdog 正 active——系统从没告诉过她。"""

    def setUp(self):
        from life import companion_awareness as awareness
        self.awareness = awareness

    def _with_systemctl(self, answers):
        original = self.awareness._systemctl
        self.awareness._systemctl = lambda *args: answers.get(args[0])
        self.addCleanup(setattr, self.awareness, '_systemctl', original)
        return self.awareness.watchdog_capability(_core(_Exec()))

    def test_active_watchdog_is_ready_and_says_it_can_rescue(self):
        item = self._with_systemctl({'is-active': 'active', 'is-enabled': 'enabled'})
        self.assertEqual(item['status'], 'ready')
        self.assertIsNone(item['reason'])
        self.assertIn('换回', item['what'], '要说清它真会做什么')
        self.assertEqual(item['what_it_does_not_do'][:2], '不动', 'must say it never rolls back data')

    def test_inactive_watchdog_says_nobody_covers_you(self):
        item = self._with_systemctl({'is-active': 'inactive', 'is-enabled': 'disabled'})
        self.assertEqual(item['status'], 'offline')
        self.assertIn('不会再自动', item['reason'] + '不会再自动换回去')

    def test_missing_systemctl_is_unknown_not_a_guess(self):
        """跑不了 systemctl 就不知道——不能断言有，也不能断言没有。"""
        item = self._with_systemctl({})
        self.assertEqual(item['status'], 'unknown')
        self.assertIn('不知道', item['what'])
        self.assertIn('不能断言', item['reason'])

    def test_guard_entry_carries_required_fields(self):
        item = self._with_systemctl({'is-active': 'active', 'is-enabled': 'enabled'})
        for key in ('capability_ref', 'category', 'description', 'node', 'status',
                    'checked_at', 'schema_ref', 'what_it_does', 'when_it_acts',
                    'what_it_does_not_do', 'limits', 'who_owns_it'):
            self.assertIn(key, item, 'guard 条目缺字段：' + key)

    def test_guard_is_in_the_catalogue(self):
        core = _core(_Exec())
        refs = {item['capability_ref']
                for item in self.awareness.capabilities(core, dict(available_actions=['command']))}
        self.assertIn('guard.watchdog', refs)


# ---------------------------------------------------------------- channel 真能发消息吗

class ChannelUsable(unittest.TestCase):
    def test_ready_channel_is_usable_and_says_so(self):
        from life import companion_awareness as awareness
        facts = dict(qq=dict(state='connected', enabled=True, value='10001', error=None,
                             observed_at='2026-11-17T23:00:00+08:00',
                             source_ref='life/companion_im.py#ImManager.snapshot',
                             source_scope='runtime_snapshot'))
        original = awareness._channel_facts
        awareness._channel_facts = lambda c, cfg: facts
        self.addCleanup(setattr, awareness, '_channel_facts', original)
        entries = {i['capability_ref']: i for i in
                   awareness.capabilities(_core(_Exec()), dict(available_actions=['command']))}
        self.assertTrue(entries['channel.qq']['usable'])
        self.assertIn('已登录', entries['channel.qq']['what'])

    def test_not_ready_channel_usable_is_false(self):
        from life import companion_awareness as awareness
        entries = {i['capability_ref']: i for i in
                   awareness.capabilities(_core(_Exec()), dict(available_actions=['command']))}
        self.assertFalse(entries['channel.qq']['usable'],
                         'channel.qq 没登录时 usable 必须是 false（ready 容易被读成「我能用 QQ」）')
        self.assertIn('发不出消息', entries['channel.qq']['what'])


class PinnedEntries(unittest.TestCase):
    """第一层索引：**每一项都给名字**，但只给 3~8 字的处境。

    设计口径（用户 2026-11-18）：「想找的时候信息都有，不用的时候不用占很大上下文」。
    依据 2026-11-18 对照实测：原设计只显示前 3 条完整原因，`guard.watchdog`
    排在第 9 位被截掉，她答「改坏了谁救」时一次都没引用它。
    """

    def setUp(self):
        from life import companion_awareness as awareness
        self.awareness = awareness
        self.core = _core(_Exec())
        self.state = dict(available_actions=['web', 'command', 'skill', 'work', 'memory',
                                             'growth', 'dsh', 'bilibili'])

    def _summary(self, **state):
        return self.awareness.capability_summary(
            self.awareness.capabilities(self.core, dict(self.state, **state)))

    def test_guard_watchdog_name_reaches_the_prompt(self):
        rows = self._summary()['unusable']
        self.assertTrue(any(row.startswith('guard.watchdog') for row in rows),
                        'guard.watchdog 的名字必须进主上下文：' + str(rows))

    def test_phone_name_reaches_the_prompt(self):
        rows = self._summary()['unusable']
        self.assertTrue(any(row.startswith('phone') for row in rows),
                        '手机的名字必须进主上下文：' + str(rows))

    def test_short_state_is_brief_not_a_full_reason(self):
        """第一层只给 3~8 字处境，完整原因不进主上下文。"""
        rows = [r for r in self._summary()['unusable'] if r.startswith('guard.watchdog')]
        self.assertTrue(rows)
        inner = rows[0][rows[0].index('（') + 1:rows[0].rindex('）')]
        self.assertLessEqual(len(inner), 10, '处境词太长，会把预算吃光：' + inner)
        self.assertNotIn('…', rows[0], '第一层不该出现被截断的长原因')

    def test_summary_still_fits_the_budget(self):
        import json
        size = len(json.dumps(self._summary(), ensure_ascii=False))
        self.assertLess(size, 1200, '摘要变长了，检查是否把不该放的都放了：%d' % size)

    def test_every_capability_name_appears_in_worst_case(self):
        """队长判据：15 条不可用的环境下，名字必须出现；每项 3-8 字；总量 < 4000。

        「名字在，原因不在」——她得先知道有这东西，才会去查。
        """
        import json
        summary = self.awareness.capability_summary(
            self.awareness.capabilities(self.core, dict(available_actions=['dsh'])))
        self.assertGreaterEqual(summary['not_ready'], 10, '这条用例要够极端才有意义')
        rows = summary['unusable'] + summary['usable']
        joined = '\n'.join(rows)
        for must in ('guard.watchdog', 'phone', 'agent.subagent', 'computer', 'body'):
            self.assertIn(must, joined, '极端环境下 %s 的名字必须出现：%s' % (must, rows))
        # 名字全在 = 条数与目录条数一致（没有任何一条被省略）
        self.assertEqual(len(rows), summary['total'], '每一项都要出现，一条都不能省')
        self.assertLess(len(json.dumps(summary, ensure_ascii=False)), 4000)

    def test_how_tells_her_where_to_look(self):
        how = self._summary()['how']
        self.assertIn('op=capabilities', how, '必须告诉她怎么查完整原因')


class PhoneReasonWording(unittest.TestCase):
    """「现在连不上」和「我没有这个能力」必须分清（对照 §5 新引入的不一致）。"""

    def setUp(self):
        from life import companion_awareness as awareness
        self.awareness = awareness

    def _reason(self, executor):
        return self.awareness._offline_reason(_core(executor), 'phone', executor=executor)

    def test_no_registration_says_offline_not_no_capability(self):
        text, scope = self._reason(_Exec(phone=dict(connected=False, devices=[])))
        self.assertEqual(scope, 'offline')
        self.assertIn('90 秒内没有上报过注册', text)
        self.assertNotIn('没有手机控制通道', text, '不能读成「这台机器没有手机能力」')
        self.assertIn('不是我没有操作手机的能力', text, '要显式区分「连不上」与「没能力」')

    def test_missing_context_says_cannot_tell_not_no_capability(self):
        class NoContext:
            def _dsh_paths(self):
                return None
        text, scope = self._reason(NoContext())
        self.assertEqual(scope, 'unknown')
        self.assertNotIn('没有手机控制通道', text, '读不到状态不等于没有能力')
        self.assertIn('读不到', text)

    def test_registered_without_stream_points_at_foreground(self):
        phone = dict(connected=True, devices=[dict(device_id='d', has_stream=False)])
        text, scope = self._reason(_Exec(phone=phone))
        self.assertEqual(scope, 'offline')
        self.assertIn('前台', text)


class UnknownMustNotBecomeAssertion(unittest.TestCase):
    """该说"不知道"的时候不许说确定的话（t5 的 F-2/F-3/F-4）。

    2026-11-18 用户看到的问题正是这一类：她该说"不知道"的地方说了假的确定的话。
    这几条钉住三件事：探测抛错 → unknown；拿不到通道状态 → 不退回配置断言；
    派活会被拒时 → status 之外必须另有一个信号说出来。
    """

    def setUp(self):
        from life import companion_awareness as awareness
        self.awareness = awareness

    def test_probe_failure_is_unknown_not_a_claim(self):
        """F-2：phone_context 抛错时不许写成「本机没有手机控制通道」。"""
        class Broken:
            def phone_context(self):
                raise RuntimeError('bridge down')
            def _dsh_paths(self):
                return None
        reason, scope = self.awareness._offline_reason(_core(Broken()), 'phone', executor=Broken())
        self.assertEqual(scope, 'unknown', '探测抛错只能说不知道')
        self.assertIn('读不到', reason)
        self.assertNotIn('没有手机控制通道', reason, '这是断言，我们并不知道')

    def test_probe_helper_separates_value_from_failure(self):
        value, failure = self.awareness._probe(lambda: 1 / 0)
        self.assertIsNone(value)
        self.assertEqual(failure, 'ZeroDivisionError')
        value, failure = self.awareness._probe(lambda: 42)
        self.assertEqual((value, failure), (42, None))

    def test_channel_snapshot_failure_does_not_fall_back_to_config(self):
        """F-3：问了通道但抛错 → unknown，不许退回配置报 disabled。"""
        temp = tempfile.TemporaryDirectory(prefix='chanf-')
        self.addCleanup(temp.cleanup)
        home = Path(temp.name)

        class BrokenBot:
            def snapshot(self):
                raise RuntimeError('im snap down')

        im = SimpleNamespace(qq=BrokenBot(), wechat=BrokenBot())
        im.snapshot = lambda: (_ for _ in ()).throw(RuntimeError('im snap down'))
        core = _core(_Exec(), home=home)
        core.executor.server = SimpleNamespace(im=im)
        facts = self.awareness._channel_facts(core, SimpleNamespace())
        for channel in ('qq', 'wechat'):
            self.assertEqual(facts[channel]['state'], 'unknown',
                             '读不到状态时不许说成「主动关的」：' + channel)
            self.assertEqual(facts[channel]['source_scope'], 'runtime_snapshot_failed')

    def test_connected_without_stream_says_dispatch_would_fail(self):
        """F-4：连着≠能派活，必须另有一个信号说出来。"""
        phone = dict(connected=True, devices=[dict(device_id='d1', has_stream=False)])
        dispatchable, caveat = self.awareness._dispatch_caveat(
            _core(_Exec(phone=phone)), 'phone', executor=_Exec(phone=phone))
        self.assertFalse(dispatchable, '没有活动连接时不能声称能派活')
        self.assertIn('前台', caveat)

    def test_connected_with_stream_is_dispatchable(self):
        phone = dict(connected=True, devices=[dict(device_id='d1', has_stream=True)])
        dispatchable, caveat = self.awareness._dispatch_caveat(
            _core(_Exec(phone=phone)), 'phone', executor=_Exec(phone=phone))
        self.assertTrue(dispatchable)
        self.assertIsNone(caveat)


    def test_self_account_also_says_unknown_when_snapshot_fails(self):
        """_self_account 读的是 _channel_facts['qq']，它必须跟着变 unknown。

        t5 专门点了这条：qq.json 关着 + snapshot 抛错时，这条路径不能仍落到 disabled。
        配置写着 enabled=false 只说明有人写过那行字，不等于此刻状态。
        """
        temp = tempfile.TemporaryDirectory(prefix='accf-')
        self.addCleanup(temp.cleanup)
        home = Path(temp.name)
        (home / 'data/companion').mkdir(parents=True)
        (home / 'data/companion/qq.json').write_text(json.dumps({'enabled': False}), encoding='utf-8')

        class BrokenBot:
            def snapshot(self):
                raise RuntimeError('im down')

        im = SimpleNamespace(qq=BrokenBot(), wechat=BrokenBot())
        im.snapshot = lambda: (_ for _ in ()).throw(RuntimeError('im down'))
        core = _core(_Exec(), home=home)
        core.executor.server = SimpleNamespace(im=im)
        account = self.awareness._self_account(core, SimpleNamespace())
        self.assertEqual(account['state'], 'unknown', '读不到就是不知道，不能说成被关了')
        self.assertEqual(account['source_scope'], 'runtime_snapshot_failed')


class UsedSummaryWording(unittest.TestCase):
    """F-5：没做过的事不许说成「用过 0 项」。"""

    def test_nothing_used_says_so_instead_of_used_zero(self):
        from life import companion_awareness as awareness
        page = dict(items=[dict(kind='command', counts=dict(total=0, ok=0, failed=0),
                                standing=dict(state='never_used'), basis='first_hand', last=None),
                           dict(kind='web', counts=dict(total=0, ok=0, failed=0),
                                standing=dict(state='never_used'), basis='first_hand', last=None)])
        text = awareness._used_summary(page, dict(reported=[]))
        self.assertNotIn('用过 0 项', text, '这会把她没做过的事说成用过')
        self.assertIn('一项都没用过', text)
        self.assertIn('只是没做过', text)


class EntryContract(unittest.TestCase):
    """条目字段契约：下游按这些键统一判断，缺键会被当成未知（t5 提的两条）。"""

    def setUp(self):
        from life import companion_awareness as awareness
        self.awareness = awareness
        self.core = _core(_Exec())

    def _catalogue(self, **state):
        return self.awareness.capabilities(self.core, dict(state))

    def test_dispatchable_semantics_are_mutually_exclusive(self):
        """None（不适用）与 False（不能派活）必须分得开，且不能靠 falsy 判断。

        t5 把这留给 t7：`dispatchable=None` 和「不在清单里 → False」相邻，
        下游写 `if not dispatchable` 会把两者一锅端。这条测试把语义钉死：
          动作 kind 在清单里 → True/False（布尔，真的回答"能派活吗"）
          非动作 kind（channel/guard）→ None（问题不成立）
        """
        for available in (['web', 'command', 'phone'], ['dsh'], []):
            catalogue = self.awareness.capabilities(self.core, dict(available_actions=available))
            for item in catalogue:
                ref, value = item['capability_ref'], item['dispatchable']
                self.assertIn(value, (True, False, None), '%s 的 dispatchable 越界：%r' % (ref, value))
                non_action = ref.startswith('channel.') or ref.startswith('guard.')
                if non_action:
                    self.assertIsNone(value, '%s 不是动作 kind，dispatchable 应为 None（不适用）' % ref)
                else:
                    self.assertIsInstance(value, bool,
                                          '%s 是动作 kind，dispatchable 必须是布尔，不能是 None' % ref)
                # ready 却派不出去，必须说得出为什么——静默的 False 比没有这个字段更糟。
                if item['status'] == 'ready' and value is False:
                    self.assertTrue(item.get('reason'), '%s 可用却派不出去，必须给原因' % ref)
    def test_every_entry_has_dispatchable_key(self):
        for available in (['web', 'command'], ['dsh'], []):
            for item in self._catalogue(available_actions=available):
                self.assertIn('dispatchable', item,
                              '%s 缺 dispatchable 键，下游会当成未知' % item['capability_ref'])

    def test_offline_entry_dispatchable_is_false_not_missing(self):
        entries = {i['capability_ref']: i for i in self._catalogue(available_actions=['command'])}
        self.assertIs(entries['action.phone']['dispatchable'], False,
                      '不在清单里 = 谈不上派活，要显式 False')

    def test_channel_state_scope_shape_unchanged(self):
        """state_scope 仍是通道状态本身；来源档位在 state_source，不混用。"""
        facts = dict(qq=dict(state='disabled', enabled=False, observed_at='2026-11-18T00:00:00+08:00',
                             source_ref='life/companion_qq.py#load_qq_settings',
                             source_scope='channel_config', value=None, error=None))
        original = self.awareness._channel_facts
        self.awareness._channel_facts = lambda c, cfg: facts
        self.addCleanup(setattr, self.awareness, '_channel_facts', original)
        entry = {i['capability_ref']: i for i in self._catalogue(available_actions=['command'])}['channel.qq']
        self.assertEqual(entry['state_scope'], 'disabled', 'state_scope 不要改格式，下游按它精确比对')
        self.assertEqual(entry['state_source'], 'channel_config', '来源档位放在 state_source')

    def test_unknown_channel_carries_why_it_does_not_know(self):
        """「不知道」必须能说明为什么不知道，否则与「被关了」分不开。"""
        facts = dict(qq=dict(state='unknown', enabled=True, observed_at='2026-11-18T00:00:00+08:00',
                             source_ref='life/companion_im.py#ImManager.snapshot',
                             source_scope='runtime_snapshot_failed', value=None, error='RuntimeError'))
        original = self.awareness._channel_facts
        self.awareness._channel_facts = lambda c, cfg: facts
        self.addCleanup(setattr, self.awareness, '_channel_facts', original)
        entry = {i['capability_ref']: i for i in self._catalogue(available_actions=['command'])}['channel.qq']
        self.assertEqual(entry['state_scope'], 'unknown')
        self.assertEqual(entry['state_source'], 'runtime_snapshot_failed')


if __name__ == '__main__':
    unittest.main()
