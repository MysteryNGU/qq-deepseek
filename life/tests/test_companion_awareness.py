# -*- coding: utf-8 -*-
"""阶段 A 认知层的离线测试：投影、账号事实、注意力、能力目录、按需读取。

这些测试不碰生产库、不联网、不起子进程：用一个只实现读接口的假 core，
把「未知就是未知」「needs_setup 必须有原因」「not_implemented 不是空数组」
这几条契约钉住。真实接线由隔离沙箱里的整体验收另行覆盖。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from life import companion_awareness as aw  # noqa: E402


class FakeRepo:
    def __init__(self, actions=None, activities=None):
        self.actions = list(actions or [])
        self.activities = list(activities or [])
        self.writes = []

    def _all(self, sql, args=()):
        if 'FROM companion_actions' in sql:
            rows = [row for row in self.actions if row['agent'] == args[0]]
            if "status IN ('pending','running')" in sql:
                rows = [row for row in rows if row['status'] in ('pending', 'running')]
            if 'project_ref=?' in sql:
                rows = [row for row in rows if row['project_ref'] == args[1]]
            return list(rows)
        return []

    def _one(self, sql, args=()):
        if 'companion_activity' in sql:
            return dict(n=sum(1 for item in self.activities
                              if item.get('project_ref') == args[1] and item.get('created', 0) > args[2]))
        if 'FROM companion_actions' in sql:
            rows = [row for row in self.actions if row['agent'] == args[0] and row.get('project_ref') == args[1]]
            return rows[0] if rows else None
        return None

    def _exec(self, sql, args=()):
        self.writes.append((sql, args))

    def tx(self):
        outer = self

        class _Ctx:
            def __enter__(self):
                return outer

            def __exit__(self, *exc):
                return False
        return _Ctx()


class FakePolicy:
    def __init__(self, enabled=False, mode=None):
        self.enabled = enabled
        if mode is not None:
            self.mode = mode


class FakeSpending:
    def __init__(self, policy):
        self.policy = policy


class FakeExecutor:
    def __init__(self, dsh=True):
        self._dsh = dsh

    def _dsh_paths(self):
        return ('/root', 'node', 'lib/bin.js') if self._dsh else None

    def available_kinds(self):
        return ['command', 'skill', 'work']


class FakeRemoteExecutor:
    """服务模式的执行器：dsh 在 original 上。"""

    def __init__(self, dsh=True):
        self.original = FakeExecutor(dsh=dsh)


class FakeCfg:
    owner = 10001
    bot_qq = 0


class FakeLife:
    agent_id = 'account-xyz'


class FakeCore:
    def __init__(self, *, home, policy=None, executor=None, drawer=None, next_wake=None,
                 actions=None, activities=None, recent=None):
        self.home = home
        self.cfg = FakeCfg()
        self.life = FakeLife()
        self.repo = FakeRepo(actions, activities)
        self.executor = executor if executor is not None else FakeExecutor()
        self.spending = FakeSpending(policy or FakePolicy())
        self._drawer = dict(drawer or {})
        self.process_started_at = 1_700_000_000.0
        self.started_at = 1_700_000_010.0
        self.active = True
        self.scope = type('S', (), dict(scope_id='account-xyz|qq:private:10001',
                                        conversation_id='qq:private:10001',
                                        participant_id='10001'))()
        self._recent = (lambda **kwargs: list(recent or []))
        if next_wake is not None:
            self._drawer['next_wake'] = next_wake

    def _state(self, key, default=None):
        return self._drawer.get(key, default)

    def _save(self, key, value):
        self._drawer[key] = value
        self.repo.writes.append(('save', (key, value)))


def make_home(tmp, qq_settings=None):
    folder = os.path.join(str(tmp), 'data', 'companion')
    os.makedirs(folder, exist_ok=True)
    if qq_settings is not None:
        with open(os.path.join(folder, 'qq.json'), 'w', encoding='utf-8') as out:
            json.dump(qq_settings, out)
    return str(tmp)


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.home = make_home(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_required_sections_have_observed_at_and_source_ref(self):
        core = FakeCore(home=self.home)
        state = dict(available_actions=['command', 'skill', 'work'])
        projection = aw.build(core, state=state, wake_kind='user')
        self.assertEqual(projection['projection_version'], aw.PROJECTION_VERSION)
        self.assertTrue(projection['observed_at'].endswith('+08:00'))
        for key in ('runtime', 'here'):
            self.assertIn('observed_at', projection[key])
            self.assertIn('source_ref', projection[key])
            self.assertIsNotNone(projection[key]['observed_at'])
        for key in ('agent', 'account'):
            self.assertIn('observed_at', projection['self'][key])
            self.assertIn('source_ref', projection['self'][key])
        self.assertEqual(projection['runtime']['available_actions'], state['available_actions'])
        self.assertEqual(projection['runtime']['home'], self.home)
        self.assertEqual(projection['here']['conversation_ref'], 'conversation:qq:private:10001')
        self.assertEqual(projection['here']['actor_ref'], 'actor:10001')
        self.assertEqual(projection['self']['account']['state'], 'disabled')
        self.assertIsNone(projection['self']['account']['value'])

    def test_build_is_read_only_and_idempotent_for_same_inputs(self):
        core = FakeCore(home=self.home)
        state = dict(available_actions=['command'])
        first = aw.build(core, state=state, now=1_700_000_500.0)
        second = aw.build(core, state=state, now=1_700_000_500.0)
        self.assertEqual(first, second)
        self.assertEqual(core.repo.writes, [])

    def test_refs_are_not_confused(self):
        core = FakeCore(home=self.home)
        here = aw.build(core)['here']
        self.assertTrue(here['actor_ref'].startswith('actor:'))
        self.assertTrue(here['account_ref'].startswith('account:'))
        self.assertTrue(here['conversation_ref'].startswith('conversation:'))
        self.assertEqual(len({here['actor_ref'], here['account_ref'], here['conversation_ref']}), 3)

    def test_projection_failure_isolated(self):
        core = FakeCore(home=self.home)
        state = dict(available_actions=['command', 'skill'])

        def boom(**kwargs):
            raise RuntimeError('kaboom')
        core._recent = boom
        projection = aw.build(core, state=state, current=type('E', (), dict(
            id='evt-1', text='你好', payload={}, received_at=time.time()))())
        self.assertEqual(projection['recent_experience'], [])
        errors = projection['projection_errors']
        self.assertTrue(any(item['section'] == 'recent_experience' for item in errors))
        # 其它段仍然完整：这一段的失败没有拖垮整份投影
        self.assertEqual(projection['runtime']['available_actions'], ['command', 'skill'])
        self.assertEqual(projection['self']['account']['state'], 'disabled')
        self.assertTrue(projection['capabilities'])


class AccountFactTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def snapshot(self, settings, state='connected', account=0):
        home = make_home(self._tmp.name + ('_%s' % id(settings)), settings)
        core = FakeCore(home=home)
        qq = type('QQ', (), {'state': state, 'snapshot': lambda self_: dict(
            **settings, state=state, error='', owner='10001', account=account)})()
        im = type('IM', (), {'qq': qq, 'snapshot': lambda self_: dict(
            channels=dict(qq=qq.snapshot()), error='')})()
        core.executor = type('EX', (), {'server': type('SRV', (), {'im': im})()})()
        return aw.build(core)['self']['account']

    def test_disabled_channel_is_not_connected(self):
        account = self.snapshot(dict(enabled=False, ws_url='ws://127.0.0.1:3001', has_token=True),
                                state='disabled')
        self.assertEqual(account['state'], 'disabled')
        self.assertIsNone(account['value'])
        self.assertNotEqual(account['state'], 'connected')

    def test_connected_requires_verified_account(self):
        account = self.snapshot(dict(enabled=True, ws_url='ws://x', has_token=True),
                                state='connected', account=0)
        self.assertEqual(account['state'], 'connected')
        # 核验数为 0：号尚未核实，值就是未知，不把配置里的 bot_qq 当成在用。
        self.assertIsNone(account['value'])

    def test_model_output_cannot_overwrite_runtime_fact(self):
        home = make_home(self._tmp.name + '_m', dict(enabled=False, ws_url='ws://x', has_token=False))
        core = FakeCore(home=home)
        # 模型能在 state 里写任何东西，运行事实仍由运行代码决定。
        state = dict(available_actions=['command'], self=dict(account=dict(state='connected', value='999')),
                     awareness=dict(self=dict(account=dict(state='connected'))))
        projection = aw.build(core, state=state)
        self.assertEqual(projection['self']['account']['state'], 'disabled')
        self.assertEqual(projection['self']['account']['source_ref'], 'life/companion_qq.py#load_qq_settings')
        self.assertNotIn('spoof', json.dumps(projection))


class AttentionTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.home = make_home(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_released_and_expiry(self):
        now = 1_700_000_000.0
        core = FakeCore(home=self.home, drawer=dict(companion_attention=[
            dict(conversation_ref='conversation:a', mode='focused', reason='主人在等答复',
                 since=now - 60, review_at=None, expires_at=None, cursor='meta:3'),
            dict(conversation_ref='conversation:b', mode='watching', reason='看群里动静',
                 since=now - 600, expires_at=now - 10),
        ]))
        rows = aw.build(core, now=now)['attention']
        self.assertEqual(rows[0]['mode'], 'focused')
        self.assertEqual(rows[0]['reason'], '主人在等答复')
        self.assertTrue(rows[0]['since'])
        self.assertEqual(rows[0]['cursor'], 'meta:3')
        self.assertEqual(rows[1]['mode'], 'released')
        self.assertTrue(rows[1]['stale'])
        self.assertEqual({row['mode'] for row in rows} <= set(aw.ATTENTION_MODES), True)

    def test_attention_set_persists_and_rejects_bad_mode(self):
        core = FakeCore(home=self.home)
        result = aw.set_attention(core, dict(arguments=json.dumps(
            dict(mode='focused', conversation_ref='conversation:a', reason='在等回执'))))
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(core._drawer['companion_attention'][0]['mode'], 'focused')
        rejected = aw.set_attention(core, dict(arguments=json.dumps(
            dict(mode='busy', conversation_ref='conversation:a'))))
        self.assertEqual(rejected['status'], 'failed')


class CapabilityTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.home = make_home(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def by_ref(self, catalogue):
        return {item['capability_ref']: item for item in catalogue}

    def test_agent_capability_is_needs_setup_with_reason(self):
        core = FakeCore(home=self.home)
        catalogue = self.by_ref(aw.capabilities(core, dict(available_actions=['command'])))
        agent = catalogue['agent.subagent']
        self.assertEqual(agent['status'], 'needs_setup')
        self.assertTrue(agent['reason'])
        for item in catalogue.values():
            for key in ('capability_ref', 'category', 'description', 'node', 'status',
                        'reason', 'checked_at', 'schema_ref'):
                self.assertIn(key, item)

    def test_dsh_ready_when_available_and_off_strict_blocks_with_reason(self):
        core = FakeCore(home=self.home, executor=FakeRemoteExecutor(dsh=True))
        catalogue = self.by_ref(aw.capabilities(core, dict(available_actions=['command'])))
        self.assertEqual(catalogue['action.dsh']['status'], 'ready')

        strict = FakeCore(home=self.home, executor=FakeRemoteExecutor(dsh=True),
                          policy=FakePolicy(enabled=True))
        catalogue = self.by_ref(aw.capabilities(strict, dict(available_actions=['command'])))
        dsh = catalogue['action.dsh']
        self.assertEqual(dsh['status'], 'needs_setup')
        self.assertIn('strict', dsh['reason'])
        self.assertEqual(strict.spending.policy.enabled, True)  # 不静默改用户的选择

    def test_bounded_estimate_keeps_dsh_ready(self):
        bounded = FakeCore(home=self.home, policy=FakePolicy(enabled=True, mode='bounded_estimate'))
        dsh = self.by_ref(aw.capabilities(bounded, dict(available_actions=['command'])))['action.dsh']
        self.assertEqual(dsh['status'], 'ready')
        self.assertEqual(aw.spending_mode(bounded.spending.policy), 'bounded_estimate')

    def test_missing_dsh_is_explained(self):
        core = FakeCore(home=self.home, executor=FakeExecutor(dsh=False))
        dsh = self.by_ref(aw.capabilities(core, dict(available_actions=['command'])))['action.dsh']
        self.assertEqual(dsh['status'], 'needs_setup')
        self.assertTrue(dsh['reason'])

    def test_needs_setup_without_reason_is_rejected(self):
        with self.assertRaises(ValueError):
            aw._capability('x', 'agent', 'd', 'needs_setup')


class ReadTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.home = make_home(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def core(self):
        now = time.time()
        return FakeCore(
            home=self.home, next_wake=now + 900,
            actions=[dict(id='act-1', agent='account-xyz', project_ref='proj-a', status='running',
                          action=json.dumps(dict(kind='dsh', goal='把报告写完')), updated=now - 30)],
            activities=[dict(project_ref='proj-a', created=now - 10)],
            recent=[dict(id='evt-0', role='user', content='上一句', created=now - 120),
                    dict(id='evt-2', role='assistant', content='我在', created=now - 60)])

    def test_read_kinds_return_uniform_envelope(self):
        core = self.core()
        for op in ('awareness', 'capabilities', 'attention', 'agent_tasks'):
            result = aw.read(core, dict(arguments=json.dumps(dict(op=op))))
            self.assertEqual(result['status'], 'completed', op)
            for key in ('items', 'next_cursor', 'source_refs', 'freshness', 'truncation'):
                self.assertIn(key, result, op)
            self.assertIn('observed_at', result['freshness'])

    def test_agent_tasks_show_real_state_and_new_results(self):
        core = self.core()
        result = aw.read(core, dict(arguments=json.dumps(dict(op='agent_tasks'))))
        item = result['items'][0]
        self.assertEqual(item['state'], 'running')
        self.assertEqual(item['new_results'], 1)
        self.assertEqual(item['task_ref'], 'act-1')
        self.assertEqual(item['node'], 'cloud-linux')

    def test_unknown_kinds_are_not_implemented_not_empty(self):
        core = self.core()
        for op in ('people', 'conversations', 'experience'):
            result = aw.read(core, dict(arguments=json.dumps(dict(op=op))))
            self.assertEqual(result['status'], 'not_implemented', op)
            self.assertTrue(result['reason'], op)
            self.assertIn('不代表', result['evidence_note'] or '不代表')

    def test_capabilities_filter_and_pagination(self):
        core = self.core()
        result = aw.read(core, dict(arguments=json.dumps(dict(op='capabilities', status='needs_setup', limit=1))))
        self.assertTrue(all(item['status'] == 'needs_setup' for item in result['items']))
        if result['next_cursor']:
            self.assertTrue(result['truncation']['truncated'])
            nxt = aw.read(core, dict(arguments=json.dumps(dict(
                op='capabilities', status='needs_setup', limit=1, cursor=result['next_cursor']))))
            self.assertNotEqual(nxt['items'], result['items'])

    def test_read_never_writes(self):
        core = self.core()
        aw.read(core, dict(arguments=json.dumps(dict(op='awareness'))))
        aw.read(core, dict(arguments=json.dumps(dict(op='attention'))))
        self.assertEqual(core.repo.writes, [])

    def test_run_uses_the_projection_already_in_the_request(self):
        core = self.core()
        stick = aw.build(core, state=dict(available_actions=['command']), now=1_700_000_000.0)
        core._last_awareness = stick
        reading = asyncio.run(aw.run(core, dict(arguments=json.dumps(dict(op='awareness')))))
        self.assertEqual(reading['status'], 'completed')
        # 读到的是这一轮请求体里的那一份，不是重新拼的
        self.assertEqual(reading['items'][0]['observed_at'], stick['observed_at'])

    def test_run_rejects_unknown_op_instead_of_leaking_to_lookup(self):
        core = self.core()
        result = asyncio.run(aw.run(core, dict(arguments=json.dumps(dict(op='search', query='码头')))))
        # search/read 不归这里管：调用方在分派前就该放过它们，到了这里是明确的拒绝。
        self.assertEqual(result['status'], 'failed')
        self.assertIn('search/read', result['output'])

    def test_attention_set_through_run(self):
        core = self.core()
        action = dict(arguments=json.dumps(dict(
            op='attention_set', conversation_ref='conversation:b', mode='watching',
            reason='等回执', expires_at=time.time() + 600)))
        result = asyncio.run(aw.run(core, action))
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(core._drawer['companion_attention'][-1]['mode'], 'watching')


class BudgetTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.home = make_home(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def size(self, projection):
        return len(json.dumps(aw.public_view(projection), ensure_ascii=False))

    def test_projection_stays_small(self):
        now = time.time()
        core = FakeCore(
            home=self.home, next_wake=now + 600,
            actions=[dict(id='act-%d' % i, agent='account-xyz', project_ref='proj-%d' % i, status='running',
                          action=json.dumps(dict(kind='dsh', goal='第%d件事' % i)), updated=now - i)
                     for i in range(8)],
            recent=[dict(id='evt-%d' % i, role='user', content='第%d句' % i, created=now - i * 30)
                    for i in range(10)])
        projection = aw.build(core, state=dict(available_actions=['command', 'skill', 'work', 'dsh',
                                                                 'memory', 'growth', 'web', 'computer']))
        # 主上下文只放摘要：这一段不能变成第二份大材料（全表留给按需读取）。
        self.assertLess(self.size(projection), 4000)
        # 摘要里必须还看得见不能用的是什么、为什么
        self.assertTrue(projection['capabilities']['not_ready'])
        self.assertIn('agent.subagent', {c['capability_ref'] for c in aw.full_catalogue(projection)
                                         if c['status'] == 'needs_setup'})

    def test_relaxed_drops_experience_and_tasks_first(self):
        now = time.time()
        core = FakeCore(home=self.home, next_wake=now + 600,
                        actions=[dict(id='a1', agent='account-xyz', project_ref='p1', status='running',
                                      action=json.dumps(dict(kind='dsh', goal='一件事')), updated=now)],
                        recent=[dict(id='e1', role='user', content='一句', created=now)])
        full = aw.build(core, state=dict(available_actions=['command']))
        lean = aw.build(core, state=dict(available_actions=['command'], _awareness_relaxed=1))
        self.assertTrue(full['recent_experience'] and full['agent_tasks'])
        self.assertEqual(lean['recent_experience'], [])
        self.assertEqual(lean['agent_tasks'], [])
        # 账号与能力事实不降级
        self.assertEqual(lean['self']['account']['state'], full['self']['account']['state'])
        self.assertEqual(lean['capabilities'], full['capabilities'])
        self.assertTrue(lean['degraded'])


class PromptTests(unittest.TestCase):
    def test_summary_states_the_rules(self):
        for phrase in ('disabled', 'not_implemented', 'needs_setup', 'released',
                       'attention_set', 'needs_setup'):
            self.assertIn(phrase, aw.SUMMARY)


if __name__ == '__main__':
    unittest.main(verbosity=2)



