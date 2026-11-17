# -*- coding: utf-8 -*-
"""经历层（t4）的离线验收：2 次成功 + 1 次失败，看它被怎么记住。

这些用例钉住五条契约，都用内存里的假台账，不碰生产库、不联网、不起进程：

1. 经历只从 ``companion_actions`` 提炼，每条都带可回查的 ``action_id``；
2. 亲历（``first_hand``）与听说（``reported``）必须分得开；
3. **一次失败不是永久结论**——失败前后成功过，就只是临时失败；从没做过才是
   ``never_used``；
4. 无命中的检索是「查了、没有」（completed），不是「没查成」（failed）；
5. 旧经历不进主上下文摘要，但仍可被按需读取召回——不静默丢，也不挤占。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from life import companion_awareness as aw  # noqa: E402
from life import companion_experience as exp  # noqa: E402
from life import companion_memory as mem  # noqa: E402

AGENT = 'acct-t4'
NOW = time.time()


def row(identity, kind, status, *, result=None, at=None, project='proj-1', **action):
    payload = dict(kind=kind)
    payload.update(action)
    return dict(id=identity, agent=AGENT, project_ref=project,
                source='evt-' + identity,
                action=json.dumps(payload, ensure_ascii=False),
                status=status,
                result=json.dumps(result or {}, ensure_ascii=False),
                created=at, updated=at)


class Ledger:
    """够用的假台账：只实现 _all/_one，按 SQL 里的关键字返回行。"""

    def __init__(self, actions=(), events=(), agent_tasks=()):
        self.actions = list(actions)
        self.events = list(events)
        self.agent_tasks = list(agent_tasks)
        self.writes = []

    def _all(self, sql, args=()):
        if 'FROM companion_actions' in sql:
            rows = [r for r in self.actions if r['agent'] == args[0]]
            if ' AND id=?' in sql:
                return [r for r in rows if r['id'] == args[1]]
            if "json_extract(action,'$.kind'),'')=?" in sql or "json_extract(action,'$.kind'),'') =?" in sql:
                rows = [r for r in rows if json.loads(r['action']).get('kind') == args[1]]
            if 'IN (' in sql and 'json_extract(action' in sql:
                kinds = {a for a in args[1:] if isinstance(a, str)}
                rows = [r for r in rows if json.loads(r['action']).get('kind') in kinds]
            if 'COALESCE(updated,created)>=' in sql:
                cut = [a for a in args if isinstance(a, float)]
                rows = [r for r in rows if (r['updated'] or r['created'] or 0) >= cut[0]]
            rows.sort(key=lambda r: r['updated'] or r['created'] or 0, reverse=True)
            limit = args[-1] if isinstance(args[-1], int) else len(rows)
            return rows[:limit]
        if 'FROM events' in sql:
            wanted = {a for a in args if isinstance(a, str)}
            out = []
            for event in self.events:
                payload = event['payload'] if isinstance(event['payload'], dict) else json.loads(event['payload'])
                if payload.get('action_id') in wanted:
                    out.append(dict(id=event['id'], payload=json.dumps(payload, ensure_ascii=False)))
            return out
        if 'companion_agent_tasks' in sql:
            refs = {a for a in args[1:] if isinstance(a, str)}
            return [r for r in self.agent_tasks if r['project_ref'] in refs]
        if 'companion_town_delegations' in sql:
            return []
        return []

    def _one(self, sql, args=()):
        rows = self._all(sql, args)
        return rows[0] if rows else None

    def _exec(self, sql, args=()):
        self.writes.append((sql, args))

    def tx(self):
        class _Ctx:
            def __enter__(self):
                return None

            def __exit__(self, *exc):
                return False
        return _Ctx()


def core_with(ledger):
    core = type('Core', (), {})()
    core.repo = ledger
    core.life = type('Life', (), dict(agent_id=AGENT))()
    core._last_awareness_context = {}
    core._last_awareness_current = None
    core._last_awareness_wake_kind = None
    core._state = lambda key, default=None: default
    core._save = lambda key, value: value
    # 直接给一份投影，避免为了一个 op 去重建整套运行事实。
    projection = aw.build(core, state=dict(available_actions=['command']), now=NOW)
    core._last_awareness = projection
    return core


def three_actions():
    """验收场景：两次成功 + 一次失败。

    command 先成功、后失败（中间的那次失败必须是临时失败）；
    web 成功一次；phone 从没成功过（只是试过）。
    """
    return [
        row('a-command-ok', 'command', 'completed', at=NOW - 3600,
            goal='看看磁盘剩多少', command='df -h /',
            result=dict(status='completed', return_code=0, output='Filesystem  Avail\n/dev/sda1  12G',
                        output_path='/opt/qq-deepseek/data/companion/artifacts/df.log')),
        row('a-command-failed', 'command', 'failed', at=NOW - 600,
            goal='再看一眼磁盘', command='df -h /',
            result=dict(status='failed', return_code=1, output='command not found: df')),
        row('a-web-ok', 'web', 'completed', at=NOW - 1200, goal='查一下天气',
            result=dict(status='completed', output='晴，12 度',
                        output_path='/opt/qq-deepseek/data/companion/artifacts/web.json')),
        row('a-phone-failed', 'phone', 'failed', at=NOW - 300, goal='点一下设置',
            result=dict(status='failed', code='phone_unavailable',
                        output='手机当前没有活动连接；先让它保持在前台，再重试')),
        row('a-desktop-ok', 'computer', 'completed', at=NOW - 1800, goal='截一张屏',
            result=dict(status='completed', output='screenshot ok')),
    ]


class ExperienceTest(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger(three_actions(),
                             events=[dict(id='evt-receipt-1',
                                          payload=dict(action_id='a-command-ok', result={}))])
        self.exp = exp.Experience(self.ledger, AGENT)

    def test_two_success_one_failure_kept_apart_by_kind(self):
        report = self.exp.by_kind()
        by_kind = {item['kind']: item for item in report['kinds']}
        self.assertEqual(by_kind['command']['counts'], dict(total=2, ok=1, failed=1))
        self.assertEqual(by_kind['web']['counts'], dict(total=1, ok=1, failed=0))
        self.assertEqual(by_kind['phone']['counts'], dict(total=1, ok=0, failed=1))
        # 每条经历都带 action_id 与可回查的读取入口。
        for item in report['kinds']:
            for episode in item['episodes']:
                self.assertTrue(episode['action_id'])
                self.assertEqual(json.loads(episode['read_op']['arguments'])['id'],
                                 episode['action_id'])
                self.assertEqual(episode['receipt_ref'], 'companion_actions:' + episode['action_id'])

    def test_one_failure_is_not_a_permanent_conclusion(self):
        by_kind = {item['kind']: item for item in self.exp.by_kind()['kinds']}
        # 先成功、后失败：只是临时失败，不能写成「这个能力不能用」。
        standing = by_kind['command']['standing']
        self.assertEqual(standing['state'], 'used_failed')
        self.assertIn('不等于这个能力不能用', standing['note'])
        self.assertIsNotNone(standing['last_ok_at'])
        # 成功的就是成功。
        self.assertEqual(by_kind['web']['standing']['state'], 'used_ok')
        # 只试过、从没成功：也不许写成「做不到」。
        self.assertEqual(by_kind['phone']['standing']['state'], 'failed_in_window')
        self.assertNotIn('不能用；', by_kind['phone']['standing']['note'])
        self.assertIn('不等于这项能力不能用', by_kind['phone']['standing']['note'])

    def test_never_used_is_said_not_hidden(self):
        report = self.exp.by_kind(kinds=['command', 'growth'])
        growth = next(item for item in report['kinds'] if item['kind'] == 'growth')
        self.assertEqual(growth['standing']['state'], 'never_used')
        self.assertEqual(growth['counts']['total'], 0)
        self.assertIn('没做过', growth['standing']['note'])
        # 问了几项就要答几项，不能因为查不到就让它消失。
        self.assertEqual({item['kind'] for item in report['kinds']}, {'command', 'growth'})

    def test_first_hand_and_reported_are_distinguished(self):
        ledger = Ledger(three_actions() + [
            row('a-dsh-ok', 'dsh', 'completed', at=NOW - 2400, goal='多步改造',
                result=dict(status='completed', output='改好了', session_id='s-1')),
        ], agent_tasks=[dict(id='companion-agent_1', project_ref='proj-1', status='completed',
                             verification='pending', updated=NOW - 2400)])
        report = exp.Experience(ledger, AGENT).by_kind()
        by_kind = {item['kind']: item for item in report['kinds']}
        self.assertEqual(by_kind['command']['basis'], exp.FIRST_HAND)
        self.assertEqual(by_kind['dsh']['basis'], exp.REPORTED)
        self.assertEqual(by_kind['dsh']['last']['basis'], exp.REPORTED)
        self.assertEqual(report['reported'][0]['basis'], exp.REPORTED)
        self.assertIn('不等于核验通过', report['reported'][0]['note'])

    def test_environmental_failure_is_flagged_not_concluded(self):
        by_kind = {item['kind']: item for item in self.exp.by_kind()['kinds']}
        episode = by_kind['phone']['last']
        self.assertTrue(episode['environmental'])
        self.assertEqual(episode['code'], 'phone_unavailable')
        self.assertIn('手机当前没有活动连接', episode['reason'])
        # 标记环境原因不等于给结论。
        self.assertEqual(by_kind['phone']['standing']['state'], 'failed_in_window')

    def test_environmental_failure_detected_by_reason_not_just_kind(self):
        """command 也可能因为环境失败（服务被关掉），不能只看 kind 白名单。"""
        ledger = Ledger([row('a-town-off', 'town', 'failed', at=NOW - 60,
                             goal='去小镇', result=dict(status='failed', code='town_disabled',
                                                        output='小镇没有启用，去不了'))])
        by_kind = {item['kind']: item for item in exp.Experience(ledger, AGENT).by_kind()['kinds']}
        self.assertTrue(by_kind['town']['last']['environmental'])
        self.assertIn('不等于', by_kind['town']['standing']['note'])

    def test_narrow_window_does_not_claim_wrong_success(self):
        """时间窗内只有失败、窗口外有成功：状态必须是 failed_in_window，不能报 used_failed 却给不出成功时间。"""
        ledger = Ledger([
            row('a-old-ok', 'command', 'completed', at=NOW - 10 * 86400, goal='很久前成功',
                result=dict(status='completed', output='ok')),
            row('a-now-fail', 'command', 'failed', at=NOW - 60, goal='现在失败',
                result=dict(status='failed', return_code=1, output='boom')),
        ])
        service = exp.Experience(ledger, AGENT)
        narrow = service.by_kind(kinds=['command'], since=NOW - 3600)['kinds'][0]
        self.assertEqual(narrow['counts'], dict(total=1, ok=0, failed=1))
        self.assertEqual(narrow['standing']['state'], 'failed_in_window')
        self.assertIsNone(narrow['standing']['last_ok_at'])
        self.assertIn('不等于这项能力不能用', narrow['standing']['note'])
        # 去掉时间窗就能看到历史上成功过——这正是要让去查的方向。
        wide = service.by_kind(kinds=['command'])['kinds'][0]
        self.assertEqual(wide['standing']['state'], 'used_failed')

    def test_tool_result_event_gives_second_source(self):
        by_kind = {item['kind']: item for item in self.exp.by_kind(kinds=['command'])['kinds']}
        episode = by_kind['command']['last']
        self.assertEqual(episode['action_id'], 'a-command-failed')
        # 失败那条没有 tool_result 事件时不编造引用。
        self.assertNotIn('tool_result_ref', episode)

    def test_summary_is_small_and_skips_old_episodes(self):
        ledger = Ledger([
            row('a-old', 'skill', 'completed', at=NOW - 40 * 86400, goal='旧的一次',
                result=dict(status='completed', output='很久以前')),
        ] + three_actions())
        service = exp.Experience(ledger, AGENT)
        summary = service.summary(['command', 'web', 'phone', 'skill'])
        kinds = {item['kind'] for item in summary['items']}
        self.assertIn('command', kinds)
        # 40 天前的旧经历不进本轮摘要……
        self.assertNotIn('skill', kinds)
        # ……但按需读取仍然查得到（不静默丢）。
        older = service.by_kind(kinds=['skill'])
        self.assertEqual(older['kinds'][0]['counts']['total'], 1)
        self.assertIn('first_hand', summary['basis_kinds'])

    def test_summary_is_bounded(self):
        many = [row('a-%d' % index, 'command', 'completed', at=NOW - index * 60,
                    goal='第 %d 次' % index, result=dict(status='completed', output='ok'))
                for index in range(30)]
        summary = exp.Experience(Ledger(many), AGENT).summary(['command'])
        # 每种能力只留最近一条，不会把 30 条都塞进来。
        self.assertEqual(len(summary['items']), 1)
        self.assertEqual(summary['items'][0]['action_id'], 'a-0')

    def test_experience_is_read_only(self):
        self.exp.by_kind()
        self.exp.summary(['command'])
        self.assertEqual(self.ledger.writes, [], '经历层不许写台账（只读）')


class MemorySearchTest(unittest.TestCase):
    def test_no_match_is_not_a_failure(self):
        lookup = mem.Lookup(Ledger(three_actions()), AGENT)
        result = asyncio.run(lookup.run(dict(kind='memory', op='search',
                                             arguments=json.dumps(dict(query='根本不存在的东西')))))
        self.assertEqual(result['status'], 'completed')
        self.assertFalse(result['matched'])
        self.assertEqual(result['count'], 0)

    def test_kind_and_window_filters(self):
        lookup = mem.Lookup(Ledger(three_actions()), AGENT)
        result = asyncio.run(lookup.run(dict(kind='memory', op='search', arguments=json.dumps(
            dict(kind='command', since=NOW - 1000)))))
        self.assertEqual(result['status'], 'completed')
        self.assertEqual([item['id'] for item in result['results']], ['a-command-failed'])
        self.assertTrue(all(item['kind'] == 'command' for item in result['results']))

    def test_unparsed_window_is_said_out_loud(self):
        lookup = mem.Lookup(Ledger(three_actions()), AGENT)
        result = asyncio.run(lookup.run(dict(kind='memory', op='search',
                                             arguments=json.dumps(dict(since='前天')))))
        self.assertEqual(result['status'], 'failed')
        self.assertIn('不能当成「查无此事」', result['output'])

    def test_read_returns_stored_output_and_timestamp(self):
        lookup = mem.Lookup(Ledger(three_actions()), AGENT)
        result = asyncio.run(lookup.run(dict(kind='memory', op='read',
                                             arguments=json.dumps(dict(id='a-command-ok')))))
        self.assertEqual(result['status'], 'completed')
        # 原文是当时存下的那段输出，不是复述。
        self.assertIn('Filesystem', result['text'])
        self.assertIn('12G', result['text'])
        self.assertEqual(result['saved_status'], 'completed')
        self.assertIsInstance(result['at_ts'], float)

    def test_unreadable_receipt_is_not_failure(self):
        ledger = Ledger([row('a-empty', 'command', 'interrupted', at=NOW - 60,
                             goal='没跑完', result=dict(status='interrupted'))])
        lookup = mem.Lookup(ledger, AGENT)
        result = asyncio.run(lookup.run(dict(kind='memory', op='read',
                                             arguments=json.dumps(dict(id='a-empty')))))
        self.assertEqual(result['status'], 'unreadable')
        self.assertIn('发生过', result['output'])


class AwarenessUsedOpTest(unittest.TestCase):
    def test_used_op_is_routed_and_answers(self):
        ledger = Ledger(three_actions())
        run = asyncio.run(aw.run(core_with(ledger), dict(
            kind='memory', op='used', arguments=json.dumps(dict(op='used', kind=['command', 'growth'])))))
        self.assertEqual(run['status'], 'completed')
        kinds = {item['kind']: item for item in run['items']}
        self.assertEqual(kinds['command']['counts']['total'], 2)
        self.assertEqual(kinds['growth']['standing']['state'], 'never_used')
        self.assertIn('用过', run['output'])
        self.assertIn('没做过', run['output'])
        self.assertEqual(run['evidence_scope'], 'action_ledger')

    def test_used_op_rejects_unparsed_window(self):
        run = asyncio.run(aw.run(core_with(Ledger()), dict(
            kind='memory', op='used', arguments=json.dumps(dict(op='used', since='上周')))))
        self.assertEqual(run['status'], 'failed')
        self.assertIn('不是「查无此事」', run['output'])

    def test_used_reports_reported_basis(self):
        ledger = Ledger(three_actions() + [
            row('a-dsh-ok', 'dsh', 'completed', at=NOW - 60, goal='多步', result=dict(status='completed')),
        ], agent_tasks=[dict(id='companion-agent_9', project_ref='proj-1', status='completed',
                             verification='pending', updated=NOW - 60)])
        run = asyncio.run(aw.run(core_with(ledger), dict(
            kind='memory', op='used', arguments=json.dumps(dict(op='used', kind=['dsh'])))))
        self.assertEqual(run['status'], 'completed')
        self.assertEqual(run['items'][0]['basis'], exp.REPORTED)
        self.assertIn('reported', run['basis_kinds'])
        self.assertTrue(run['reported'])

    def test_used_is_registered_as_read_op(self):
        self.assertIn('used', aw.READ_OPS)
        self.assertTrue(aw._used.__doc__)


class WiringRegressionTest(unittest.TestCase):
    """钉住真实调用点的形状——2026-11-18 每轮失声就是这里写错了。

    生产事故：`_messages` 里写成了 `experience.summary(kinds)`，但 `summary`
    是 `Experience` 的方法、模块级没有它，于是每一轮 `AttributeError`，
    认知整轮不发声（2 秒一次，对话轮与后台轮都一样）。

    这类错 import 测试抓不住，只有对着**真实调用形状**跑才抓得住，所以这里
    直接按 `life/companion.py` 的写法调用一次，失败即断言失败。
    """

    def test_call_shape_matches_production_call_site(self):
        ledger = Ledger(three_actions())
        # 与 companion.py 的调用完全同形：<module>.Experience(repo, agent_id).summary(kinds)
        service = exp.Experience(ledger, AGENT)
        result = service.summary(['command', 'web'])
        self.assertIsInstance(result, dict)
        self.assertIn('items', result)
        self.assertIn('basis_kinds', result)
        # 模块级别没有同名函数：有的话说明又会出现两种写法并存。
        self.assertFalse(hasattr(exp, 'summary'),
                         '模块级不该有 summary()，否则调用点容易写成 experience.summary(...)')

    def test_companion_module_binds_experience_log(self):
        """companion.py 引入经历层用的名字必须与模块级对象不冲突。

        同文件里 `experience` 这个名字已经被 `self.life.experience` 占用，
        所以模块级引入必须叫别的名字，否则 `_messages` 里会拿到另一个对象。
        """
        import ast
        source = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), 'companion.py'), encoding='utf-8').read()
        tree = ast.parse(source)
        imports = [node for node in ast.walk(tree)
                   if isinstance(node, ast.ImportFrom)
                   and any(alias.name == 'companion_experience' for alias in node.names)]
        self.assertTrue(imports, 'companion.py 必须引入经历层')
        alias = imports[0].names[0].asname
        self.assertNotEqual(alias, 'experience',
                            '模块级别名不能叫 experience，会和 self.life.experience 撞名')
        calls = [node for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                 and isinstance(node.func.value, ast.Name)
                 and node.func.value.id == alias and node.func.attr == 'summary']
        self.assertFalse(calls, '经历摘要必须走 Experience(...).summary(kinds)，不能是 %s.summary(...)' % alias)


if __name__ == '__main__':
    unittest.main(verbosity=2)
