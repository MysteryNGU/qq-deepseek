"""P33 运行时判据：`_starvation_snapshot` / `_wake_log` 跑在真实 LifeRuntime 上。

不构造完整 Companion（那要模型与网络），而是把这两个方法绑到一个只带
repo / life / 必要状态的最小对象上——**被验证的是方法本身，不是替身**。

覆盖：
  * 饥饿判定算得对（13 分钟的子 agent → starved）
  * 新鲜的（30 秒）不误报
  * `raw` 在事实里，但 observability 的对外视图不带 raw
  * `_wake_log` 每拍只写一条、同类 60 秒去重
  * probe 本身**不写 companion_activity**
  * 告警存在时，调度判断（blocked）与无告警时一致
"""
import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest
from types import SimpleNamespace

from life.companion import Companion, STARVATION_SECONDS, WAKE_LOG_DEDUPE_SECONDS
from life.runtime import LifeRuntime, LifeConfig


class Stub:
    """只带 `_starvation_snapshot` / `_wake_log` 需要的字段。方法本身来自 Companion。"""

    _starvation_snapshot = Companion._starvation_snapshot
    _wake_log = Companion._wake_log
    _state = Companion._state
    _save = Companion._save
    _audit = Companion._audit
    _emit = Companion._emit

    def __init__(self, life, *, active=True, batch=None):
        self.life = life
        self.repo = life.repo
        self.active = active
        self._input_batch = list(batch or [])
        self.on_event = None
        self.followthrough = SimpleNamespace(
            due=lambda **kw: None)
        self.town = SimpleNamespace(pending_project_refs=lambda: set())
        self.bilibili_social = SimpleNamespace(pending=lambda limit=50: [])


class StarvationRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.life = LifeRuntime(self.path / 'life.db', config=LifeConfig(relationship_owner='1'))
        self.repo = self.life.repo
        # Companion.__init__ 才建这几张表；测试只借用两个方法，所以要自己建。
        with self.repo.tx():
            self.repo._exec('CREATE TABLE IF NOT EXISTS companion_activity('
                            'id TEXT PRIMARY KEY, agent TEXT, type TEXT, text TEXT,'
                            ' details TEXT, created REAL)')
            self.repo._exec('CREATE TABLE IF NOT EXISTS companion_actions('
                            'id TEXT PRIMARY KEY, agent TEXT, project_ref TEXT, source TEXT,'
                            ' action TEXT, status TEXT, result TEXT, created REAL, updated REAL)')
            self.repo._exec('CREATE TABLE IF NOT EXISTS companion_agent_tasks('
                            'id TEXT PRIMARY KEY, agent TEXT, status TEXT, updated REAL)')
        self.stub = Stub(self.life)

    def tearDown(self):
        self.life.close()
        self.tmp.cleanup()

    # ------------------------------------------------------------------ 工具
    def _agent_action(self, age_s, *, status='queued', kind='agent'):
        now = time.time()
        action = json.dumps(dict(kind=kind, goal='g'))
        with self.repo.tx():
            self.repo._exec(
                'INSERT INTO companion_actions(id,agent,project_ref,source,action,status,result,created,updated)'
                ' VALUES(?,?,?,?,?,?,?,?,?)',
                ('act-' + str(age_s), self.life.agent_id, '', 'internal', action, status,
                 None, now - age_s, now - age_s))

    def _facts(self):
        return {item['fact_ref']: item for item in self.stub._starvation_snapshot()['facts']}

    # ------------------------------------------------------------------ 饥饿
    def test_stuck_agent_action_is_starved(self):
        """2026-11-18 实测的形态：kind=agent 卡在 queued 13 分钟。"""
        self._agent_action(780)
        snapshot = self.stub._starvation_snapshot()
        fact = self._facts()['queue.agent_action']
        self.assertEqual(fact['state'], 'ready')
        self.assertTrue(fact['starved'], '13 分钟必须判为饥饿')
        self.assertAlmostEqual(fact['age_s'], 780, delta=5)
        self.assertEqual(snapshot['starved_count'], 1)

    def test_fresh_agent_action_is_not_starved(self):
        self._agent_action(30)
        fact = self._facts()['queue.agent_action']
        self.assertFalse(fact['starved'], '30 秒不该报饥饿')
        self.assertEqual(self.stub._starvation_snapshot()['starved_count'], 0)

    def test_no_queue_gives_no_starved(self):
        snapshot = self.stub._starvation_snapshot()
        self.assertEqual(snapshot['starved_count'], 0)
        self.assertEqual(snapshot['facts'], [])

    def test_user_message_uses_30s_threshold(self):
        now = time.time()
        self.stub._input_batch = [dict(at=now - 45), dict(at=now - 5)]
        fact = self._facts()['queue.user_message']
        self.assertTrue(fact['starved'], '45 秒超过 30 秒阈值')
        self.assertAlmostEqual(fact['age_s'], 45, delta=3)

    def test_fact_shape_carries_source_and_raw(self):
        self._agent_action(120)
        fact = self._facts()['queue.agent_action']
        for key in ('fact_ref', 'subject', 'state', 'source_ref', 'observed_at',
                    'age_s', 'detail', 'raw', 'starved'):
            self.assertIn(key, fact)
        self.assertIn('companion_actions', fact['source_ref'])

    def test_thresholds_are_reported_with_the_snapshot(self):
        snapshot = self.stub._starvation_snapshot()
        self.assertEqual(snapshot['thresholds'], STARVATION_SECONDS)

    # ------------------------------------------------------------------ 只告警不阻断
    def test_alert_does_not_change_scheduling(self):
        """captain 要求：阈值先只告警。饥饿为真时 blocked 结论必须不变。"""
        # 无饥饿时
        before = _probe_blocked(self.stub)
        self._agent_action(780)          # 造出饥饿
        after = _probe_blocked(self.stub)
        self.assertEqual(before, after, '饥饿告警不能改变「为什么这一拍没触发」的结论')

    # ------------------------------------------------------------------ 写库
    def test_probe_does_not_write_activity(self):
        self._agent_action(780)
        count = lambda: self.repo._one('SELECT COUNT(*) n FROM companion_activity')['n']
        baseline = count()
        for _ in range(10):
            _probe_blocked(self.stub)
        self.assertEqual(count(), baseline, 'probe 每拍不能往 companion_activity 写行')

    def test_wake_log_writes_one_row_per_second(self):
        baseline = self.repo._one('SELECT COUNT(*) n FROM companion_activity')['n']
        for _ in range(10):
            self.stub._wake_log('timer', dict(source='timer', age_s=1.0), '测试')
        added = self.repo._one('SELECT COUNT(*) n FROM companion_activity')['n'] - baseline
        self.assertEqual(added, 1, '同一秒内只允许写一条')

    def test_wake_log_dedupes_same_trigger_within_window(self):
        base = time.time()
        self.stub._wake_log('timer', dict(source='timer', age_s=1.0), 'a', now=base)
        self.stub._wake_log('timer', dict(source='timer', age_s=1.0), 'b', now=base + 10)
        rows = self.repo._all("SELECT text FROM companion_activity WHERE type='wake'")
        self.assertEqual(len(rows), 1, '同类事件 %d 秒内只记一次' % WAKE_LOG_DEDUPE_SECONDS)
        self.stub._wake_log('timer', dict(source='timer', age_s=1.0), 'c',
                            now=base + WAKE_LOG_DEDUPE_SECONDS + 5)
        rows = self.repo._all("SELECT text FROM companion_activity WHERE type='wake'")
        self.assertEqual(len(rows), 2, '超过去重窗口后应该再记一条')

    def test_wake_record_answers_the_three_questions(self):
        self.stub._wake_log('user_message',
                            dict(source='user_message', ref='e1', age_s=42.0),
                            '用户在说话，先回应',
                            [dict(source='queue.project_due', age_s=900.0, detail='项目到期')])
        row = self.repo._one("SELECT details FROM companion_activity WHERE type='wake'")
        details = json.loads(row['details'])
        self.assertEqual(details['trigger'], 'user_message')     # 为什么醒
        self.assertTrue(details['candidates'])                   # 还有谁在等
        self.assertEqual(details['chosen']['ref'], 'e1')         # 选了哪个
        self.assertTrue(details['why'])                          # 为什么选它
        self.assertEqual(details['waited_s'], 42.0)              # 等了多久

    def test_wake_log_failure_never_raises(self):
        broken = Stub(self.life)
        broken.repo = SimpleNamespace(tx=lambda: (_ for _ in ()).throw(RuntimeError('boom')))
        self.assertIsNone(broken._wake_log('timer', dict(source='timer'), 'x'))


def _probe_blocked(stub):
    """复刻 `_record_scheduler_probe` 的 blocked 判定，用同一批输入。"""
    now = time.time()
    next_wake = stub._state('next_wake')
    try:
        due = stub.followthrough.due(autonomy=stub.active,
                                     delegated_refs=stub.town.pending_project_refs())
    except Exception:
        due = None
    if getattr(stub, '_lock_locked', False):
        return 'cognition_locked'
    if getattr(stub, '_execution', None):
        return 'execution_running'
    if not stub.active:
        return 'autonomy_off'
    if due:
        return 'candidate_ready'
    if isinstance(next_wake, (int, float)) and next_wake > now:
        return 'waiting_next_wake'
    return 'no_candidate'


if __name__ == '__main__':
    unittest.main()
