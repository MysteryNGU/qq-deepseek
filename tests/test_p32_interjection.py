# -*- coding: utf-8 -*-
"""P32：插话感知与气泡收回——验 `life/companion_p32.py` 的纯函数。

为什么不直接测 Companion：`companion.py` 今晚被多人反复整文件重写（同一文件上
改动互相覆盖了三次）。把能独立的部分放进自己的文件、用纯函数钉死契约，
这样**逻辑对不对可以独立验证**，接线只需要在 companion.py 里加两行。

跑法：python -B -X utf8 -m unittest tests.test_p32_interjection -v
"""
from __future__ import annotations

from pathlib import Path
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from life import companion_p32 as p32  # noqa: E402


class InterjectionView(unittest.TestCase):
    """需求 1：她在回答时用户又说了话，下一轮要看见。"""

    def setUp(self):
        self.now = time.time()

    def _rows(self, *texts, age=0):
        return [dict(text=text, at=self.now - age, source='mobile') for text in texts]

    def test_no_interjection_returns_none(self):
        self.assertIsNone(p32.interjection_view([]), '没有插话时不要放空字段')

    def test_renders_the_actual_words_not_just_a_count(self):
        view = p32.interjection_view(self._rows('等等，换个话题'), now=self.now)
        self.assertIn('等等，换个话题', view['text'], '要知道用户说了什么，只看条数没用')
        self.assertEqual(view['count'], 1)
        self.assertFalse(view['stale'])

    def test_five_messages_all_listed_within_budget(self):
        view = p32.interjection_view(self._rows(*['第%d句' % i for i in range(1, 6)]), now=self.now)
        for i in range(1, 6):
            self.assertIn('第%d句' % i, view['text'])
        self.assertLessEqual(len(view['text']), 600, '这一段有硬预算')

    def test_beyond_five_keeps_a_count_not_silently_dropped(self):
        view = p32.interjection_view(self._rows(*['第%d句' % i for i in range(1, 9)]), now=self.now)
        self.assertIn('另有 3 条', view['text'], '超出部分给条数，不许静默丢')
        self.assertEqual(view['count'], 8)

    def test_long_text_is_truncated(self):
        view = p32.interjection_view(self._rows('很长的句子' * 100), now=self.now)
        self.assertLessEqual(len(view['text']), 600)

    def test_stale_interjection_is_not_presented_as_just_now(self):
        """过了时效还按原样呈现会误导——必须说成「较早的插话」。"""
        view = p32.interjection_view(self._rows('很久以前那句', age=400), now=self.now)
        self.assertTrue(view['stale'])
        self.assertIn('较早', view['text'])
        self.assertIn('分钟前', view['text'])

    def test_recent_shows_how_long_ago(self):
        view = p32.interjection_view(self._rows('两分钟前那句', age=130), now=self.now)
        self.assertIn('2 分钟前', view['text'])
        self.assertFalse(view['stale'], '2 分钟还不算过期')

    def test_blank_rows_ignored(self):
        rows = [dict(text='   ', at=self.now), dict(text='有内容', at=self.now)]
        view = p32.interjection_view(rows, now=self.now)
        self.assertEqual(view['count'], 1)

    def test_never_raises_on_junk(self):
        for junk in (None, [], [None], ['x'], [dict()], [dict(text=None)]):
            self.assertIsNone(p32.interjection_view(junk, now=self.now))


class ShouldRecord(unittest.TestCase):
    """只在「她正在回答」时记插话，且排除内部来源。"""

    def test_records_only_while_answering(self):
        self.assertTrue(p32.should_record_interjection(answering=True, text='你在吗', source_kind='mobile'))
        self.assertFalse(p32.should_record_interjection(answering=False, text='你在吗', source_kind='mobile'),
                         '她没在回答时，那就是普通新消息，不是插话')

    def test_ignores_internal_and_blank(self):
        self.assertFalse(p32.should_record_interjection(answering=True, text='x', source_kind='internal'))
        self.assertFalse(p32.should_record_interjection(answering=True, text='   ', source_kind='mobile'))


class DecideBubbles(unittest.TestCase):
    """需求 2：模型自己收回没发的气泡。"""

    def test_silence_keeps_first_and_drops_rest(self):
        keep, dropped, silenced = p32.decide_bubbles(['第一句', '第二句', '第三句'], silence_pending=True)
        self.assertEqual(keep, ['第一句'])
        self.assertEqual([row['text'] for row in dropped], ['第二句', '第三句'])
        self.assertTrue(silenced)

    def test_without_silence_everything_is_kept(self):
        keep, dropped, silenced = p32.decide_bubbles(['a', 'b', 'c'], silence_pending=False)
        self.assertEqual(keep, ['a', 'b', 'c'])
        self.assertEqual(dropped, [])
        self.assertFalse(silenced)

    def test_single_bubble_is_never_silenced(self):
        """只有一条时收回等于整轮不说话——那不是「剩下不发了」，是另一件事。"""
        keep, dropped, silenced = p32.decide_bubbles(['就一句'], silence_pending=True)
        self.assertEqual(keep, ['就一句'])
        self.assertFalse(silenced)

    def test_blank_bubbles_do_not_count(self):
        keep, dropped, silenced = p32.decide_bubbles(['第一句', '   ', '第三句'], silence_pending=True)
        self.assertEqual(keep, ['第一句'])
        self.assertEqual([row['text'] for row in dropped], ['第三句'], '空白不算一条气泡')

    def test_dropped_parts_keep_their_index(self):
        _keep, dropped, _ = p32.decide_bubbles(['a', 'b', 'c'], silence_pending=True)
        self.assertEqual([row['part'] for row in dropped], [1, 2], '取消的编号要能对上原文')


class SilenceAudit(unittest.TestCase):
    """不许静默吞掉：取消了哪几条要留痕。"""

    def test_audit_says_what_was_dropped(self):
        _keep, dropped, _ = p32.decide_bubbles(['a', 'b', 'c'], silence_pending=True)
        audit = p32.silence_audit(dropped, kept=1)
        self.assertEqual(audit['details']['reason'], 'silence_pending')
        self.assertEqual(audit['details']['dropped'], 2)
        self.assertEqual(audit['details']['kept'], 1)
        self.assertEqual(audit['details']['dropped_preview'], ['b', 'c'])
        self.assertIn('收回', audit['text'])


class Budget(unittest.TestCase):
    """主上下文预算：插话这一段必须有硬上限。"""

    def test_worst_case_is_bounded(self):
        now = time.time()
        worst = [dict(text='很长的用户输入' * 200, at=now) for _ in range(50)]
        view = p32.interjection_view(worst, now=now)
        self.assertLessEqual(len(view['text']), p32.INTERJECTION_MAX_CHARS)
        self.assertLessEqual(p32.INTERJECTION_MAX_CHARS, 600, '这一段不许超过 600 字符')


if __name__ == '__main__':
    unittest.main()
