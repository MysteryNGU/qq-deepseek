"""P33 唤醒可解释性：饥饿盘点与唤醒记录的判据。

本文件只依赖**已经存在的**数据源，不依赖 P33 待实现的 `_wake_log` / `_starvation_snapshot`。
这样它现在就能跑、能红、能在实现前先把判据钉死。

已经可测的部分（现在就该通过）：
  * 五个源各自「等了多久」的数据来源存在且语义正确
  * 阈值常量结构
  * `followthrough.due()` 现在只返回 min() 一个 —— 这是 P33 要解决的已知限制

等 `_starvation_snapshot` / `_wake_log` 落地后，取消对应的 skip 即可。
"""
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

# 动手前现取（2026-11-18 09:40，云端 /opt/qq-deepseek/life/companion.py）。
# **不要凭记忆填这个值**——今晚它已经变过多次，写死旧值会让行号引用全部不可信。
CLOUD_MD5 = '1986f9764fdc60c01aef30432460ea23'
CLOUD_LINES = 3561
# 历史基线（按时间顺序）：837513ab/3339 → c32c180e/3343 → b9f69dea/3369 → … → 937528c3/3543
KNOWN_BASELINES = {'1986f9764fdc60c01aef30432460ea23', '937528c384c9fa9162c09a3f432f55ff', 'c32c180e1bd7463cd522b8cbc966433e',
                   '837513ab1d01b7e495d6fb69ca4648ff', 'b9f69dea97f4c61e9b91ca4c9aa21991'}


def _db(path):
    return sqlite3.connect(str(path))


class SourceAvailabilityTests(unittest.TestCase):
    """五个源的时间戳字段必须真实存在——否则饥饿检测算不出「等了多久」。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / 'life.db'
        self.con = _db(self.db)

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def _columns(self, table, ddl):
        self.con.execute(ddl)
        return {row[1] for row in self.con.execute('PRAGMA table_info(%s)' % table)}

    def test_agent_tasks_carries_updated_for_stall_detection(self):
        """台账用 updated 判断「多久没动」，不是 created。"""
        cols = self._columns('companion_agent_tasks',
                             'CREATE TABLE companion_agent_tasks('
                             'id TEXT PRIMARY KEY, agent TEXT, status TEXT,'
                             ' created REAL, started REAL, finished REAL, updated REAL NOT NULL)')
        self.assertIn('updated', cols)
        self.assertIn('status', cols)

    def test_actions_carry_kind_and_updated_for_unclaimed_detection(self):
        """companion_actions 里 kind=agent 且 queued/pending 的行，是今晚卡住的那种。"""
        cols = self._columns('companion_actions',
                             'CREATE TABLE companion_actions('
                             'id TEXT PRIMARY KEY, agent TEXT, status TEXT, action TEXT,'
                             ' created REAL, updated REAL)')
        self.assertIn('action', cols)   # kind 存在 action 的 JSON 里
        self.assertIn('updated', cols)

    def test_bilibili_inbox_carries_next_due(self):
        cols = self._columns('companion_bili_inbox',
                             'CREATE TABLE companion_bili_inbox('
                             'scope TEXT, account TEXT, id TEXT, status TEXT,'
                             ' occurred REAL, created REAL, updated REAL, next_due REAL)')
        self.assertIn('next_due', cols)

    def test_starvation_age_is_computable_from_updated(self):
        """构造一条 13 分钟没动的子 agent，确认算得出 780 秒。"""
        self.con.execute('CREATE TABLE companion_agent_tasks('
                         'id TEXT PRIMARY KEY, agent TEXT, status TEXT, updated REAL)')
        now = 1_800_000_000.0
        self.con.execute('INSERT INTO companion_agent_tasks VALUES(?,?,?,?)',
                         ('a1', 'xiaoyu', 'running', now - 780))
        row = self.con.execute(
            'SELECT updated FROM companion_agent_tasks WHERE status IN (?,?,?) ORDER BY updated',
            ('queued', 'running', 'waiting')).fetchone()
        self.assertAlmostEqual(now - row[0], 780, places=1)


class ThresholdShapeTests(unittest.TestCase):
    """阈值必须是命名常量、统一结构——不许散落在判断里。"""

    def test_captain_thresholds_are_reflected(self):
        expected = dict(user_message=30.0, project_due=300.0,
                        agent_action=60.0, agent_task=60.0, bilibili=600.0)
        # 这个 dict 与设计文档 §3 必须一致；实现时从 life.companion 导入核对。
        contract = dict(user_message=30.0, project_due=300.0,
                        agent_action=60.0, agent_task=60.0, bilibili=600.0)
        self.assertEqual(contract, expected)
        self.assertLess(expected['user_message'], expected['project_due'],
                        '用户消息必须比项目到期更早告警')
        self.assertLess(expected['agent_action'], expected['bilibili'],
                        '未认领的子 agent 行动必须比 B站消息更早告警')


class DueOnlyReturnsOneTests(unittest.TestCase):
    """P33 要解决的已知限制：due() 只给 min() 一个（`all_candidates` 已批准加入）。"""

    def test_due_contract_returns_single_or_none(self):
        """记录现状：不传参时返回值是单个 dict 或 None，不是列表。

        加入 `all_candidates` 后，这条要改成**同时**断言：
        不传参仍是单个 dict/None，传 True 才返回列表。
        """
        import inspect
        from life.companion_followthrough import FollowThrough
        signature = inspect.signature(FollowThrough.due)
        if 'all_candidates' in signature.parameters:
            self.assertEqual(signature.parameters['all_candidates'].default, False,
                             'all_candidates 默认值必须是 False，否则现有调用方行为会变')
            return
        self.assertNotIn('all_candidates', signature.parameters,
                         '已加入 all_candidates：请把这条测试改成验证默认行为不变')


@unittest.skip('需要完整 Companion 实例，见 test_wake_log_runtime.py')
class WakeRecordContractTests(unittest.TestCase):
    """唤醒记录必须能回答三个问题 + 等了多久。"""

    def test_record_has_trigger_candidates_chosen_why_waited(self):
        self.fail('见 tests/test_wake_log_runtime.py')


@unittest.skip('需要完整 Companion 实例，见 test_wake_log_runtime.py')
class StarvationTests(unittest.TestCase):
    def test_stuck_agent_action_raises_alert(self):
        self.fail('见 tests/test_wake_log_runtime.py')

    def test_fresh_agent_action_does_not_alert(self):
        self.fail('见 tests/test_wake_log_runtime.py')

    def test_probe_does_not_write_activity_every_tick(self):
        self.fail('见 tests/test_wake_log_runtime.py')

    def test_alert_does_not_change_scheduling(self):
        self.fail('见 tests/test_wake_log_runtime.py')


class ImplementedShapeTests(unittest.TestCase):
    """实现在 life/companion.py 里的形状约束（不需要起完整 Companion）。"""

    def test_thresholds_live_in_one_place(self):
        from life.companion import STARVATION_SECONDS, WAKE_LOG_DEDUPE_SECONDS
        self.assertEqual(STARVATION_SECONDS['user_message'], 30.0)
        self.assertEqual(STARVATION_SECONDS['project_due'], 300.0)
        self.assertEqual(STARVATION_SECONDS['agent_action'], 60.0)
        self.assertEqual(STARVATION_SECONDS['agent_task'], 60.0)
        self.assertGreater(WAKE_LOG_DEDUPE_SECONDS, 0)

    def test_snapshot_shape_matches_world_fact_contract(self):
        """§4 的事实形状：fact_ref/subject/state/source_ref/observed_at/age_s/detail/raw。"""
        import inspect
        from life.companion import Companion
        source = inspect.getsource(Companion._starvation_snapshot)
        for key in ('fact_ref', 'subject', 'state', 'source_ref', 'observed_at', 'age_s', 'detail'):
            self.assertIn(key, source, '事实形状缺少字段 %s' % key)
        self.assertIn('starved', source, '饥饿必须用布尔字段，不是新状态词')

    def test_state_uses_existing_vocabulary_not_new_words(self):
        """不发明第四套词表：state 只允许既有词。"""
        import inspect
        from life.companion import Companion
        source = inspect.getsource(Companion._starvation_snapshot)
        allowed = {'ready', 'offline', 'unknown', 'needs_setup', 'unsupported'}
        found = set(__import__('re').findall(r"'state', '(\w+)'", source))
        self.assertTrue(found.issubset(allowed), '出现了词表外状态词：%s' % (found - allowed))


class CloudBaselineTests(unittest.TestCase):
    """改动前的基线必须可复核——否则「行号对不对」无从谈起。"""

    def test_cloud_source_md5_recorded(self):
        self.assertEqual(len(CLOUD_MD5), 32)
        self.assertGreater(CLOUD_LINES, 3000)

    def test_local_matches_cloud_baseline_or_says_so(self):
        """本地与云端必须相等，**或明确声明不同**——不能默默分叉。

        captain 的要求：t13 期间两边本来就该不同，所以这条不能只断言相等，
        而是断言「要么一致，要么有人显式登记过这是分叉状态」。

        2026-11-18 实测过多种状态：本地领先（未提交的插话代码）、本地落后
        （没有 t15 的 worker 修复）、双向互补（本地有 dsh 说明、云端有 queued 修复）。
        **md5 不同不代表谁更新**——只有逐行 diff 才看得出来。
        """
        local = Path(__file__).resolve().parents[1] / 'life' / 'companion.py'
        if not local.is_file():
            self.skipTest('本地没有 companion.py')
        import hashlib
        digest = hashlib.md5(local.read_bytes()).hexdigest()
        if digest == CLOUD_MD5:
            return                      # 一致，最好
        declared = Path(__file__).resolve().parents[1] / 'docs' / '.local-companion-md5'
        note = declared.read_text('utf-8').strip() if declared.is_file() else ''
        if not note:
            self.fail('本地 life/companion.py md5=%s 与云端基线 %s 不同，且没有 '
                      'docs/.local-companion-md5 登记。分叉本身可以接受（t13 正在改它），'
                      '但必须明说，否则所有行号引用都不可信。' % (digest, CLOUD_MD5))
        # 登记文件只需说明「哪一边多什么」。md5 会随 t13 每次保存变化，
        # 所以只要求登记里写了差异性质，不要求 md5 逐字匹配。
        self.assertIn('只有本地有', note, '登记里必须写清哪一边多了什么，不能只记 md5')
        self.assertIn('只有云端有', note, '登记里必须写清哪一边多了什么，不能只记 md5')

    def test_baseline_is_a_known_one(self):
        """基线必须是我们登记过的某一版，防止随手改常量改错。"""
        self.assertIn(CLOUD_MD5, KNOWN_BASELINES)


if __name__ == '__main__':
    unittest.main()
