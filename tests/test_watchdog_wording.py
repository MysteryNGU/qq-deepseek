# -*- coding: utf-8 -*-
"""P34 / t7 R-03：watchdog 条目的措辞必须随状态变化。

缺陷原形：`status` 和 `reason` 说的是「这个守护停了，要人手动处理」，
但 `what_it_does` 无条件写「起不来就换回上次能跑的源码版本」——
用户按需读取（`memory op=capabilities`）时看到的是「我有保障」，实际没人管。

判据（任务书）：
1. inactive 时 `what_it_does` 不能让人以为「改坏了会自动恢复」
2. 读完整条目时能明确知道「现在没人管」
3. 不靠注释/文档，条目内容本身要说清

跑法：python -B -X utf8 -m unittest tests.test_watchdog_wording -v
"""
from __future__ import annotations

from pathlib import Path
import sys
import unittest
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from life import companion_awareness as awareness  # noqa: E402

# 一旦出现这些词，就说明这条被读成了「有保障 / 会自动回滚」。
GUARANTEE_WORDS = ('会自动回滚', '会自动恢复', '会替我兜底', '有保障')
# 「不会自动换回去」是**否定**，是正确的表述，不能当违规——所以单独处理。
NEGATED = ('不会', '没能', '不确定', '无从', '并不')


def promises_guarantee(text):
    """这条措辞里有没有**没有被否定**的保证性说法。"""
    hits = []
    for word in GUARANTEE_WORDS:
        index = 0
        while True:
            index = text.find(word, index)
            if index < 0:
                break
            before = text[max(0, index - 6):index]
            if not any(neg in before for neg in NEGATED):
                hits.append(word)
            index += len(word)
    return hits


def entry(answers):
    """用给定的 systemctl 回答造一条 guard.watchdog。"""
    original = awareness._systemctl
    awareness._systemctl = lambda *args: answers.get(args[0])
    try:
        return awareness.watchdog_capability(SimpleNamespace(executor=None))
    finally:
        awareness._systemctl = original


class Inactive(unittest.TestCase):
    def setUp(self):
        self.item = entry({'is-active': 'inactive', 'is-enabled': 'disabled'})

    def test_status_and_reason_still_correct(self):
        self.assertEqual(self.item['status'], 'offline')
        self.assertIn('停了', self.item['reason'])
        self.assertIn('手动', self.item['reason'])

    def test_what_it_does_leads_with_not_running(self):
        """最重要的一条：开头必须说清「现在没在做事」，不能一上来讲能力。"""
        text = self.item['what_it_does']
        self.assertIn('现在没有在做事', text)
        self.assertLess(text.index('现在没有在做事'), text.index('它本来能做的事'),
                        '先说现状再说能力，避免只读半句就以为有保障')

    def test_what_it_does_says_it_will_do_nothing(self):
        text = self.item['what_it_does']
        self.assertIn('不会替我做任何事', text)
        self.assertIn('手动恢复', text)

    def test_no_guarantee_language_anywhere_in_entry(self):
        for field in ('what_it_does', 'when_it_acts', 'limits', 'what_it_does_not_do', 'what', 'reason'):
            hits = promises_guarantee(str(self.item.get(field) or ''))
            self.assertEqual(hits, [], '%s 里出现了没被否定的保证性说法：%s' % (field, hits))

    def test_when_it_acts_says_never(self):
        self.assertIn('任何时候都不会动', self.item['when_it_acts'])

    def test_limits_says_no_coverage(self):
        self.assertIn('没有兜底', self.item['limits'])

    def test_summary_line_says_nobody_covers(self):
        self.assertIn('没人', self.item['what'])


class Active(unittest.TestCase):
    def setUp(self):
        self.item = entry({'is-active': 'active', 'is-enabled': 'enabled'})

    def test_ready_and_describes_rescue(self):
        self.assertEqual(self.item['status'], 'ready')
        self.assertIsNone(self.item['reason'])
        self.assertIn('换回', self.item['what_it_does'])
        self.assertIn('重启', self.item['what_it_does'])

    def test_still_says_what_it_never_does(self):
        self.assertIn('不动', self.item['what_it_does_not_do'])
        self.assertIn('life.db', self.item['what_it_does_not_do'])

    def test_limits_kept(self):
        self.assertIn('3 次', self.item['limits'])


class Unknown(unittest.TestCase):
    def setUp(self):
        self.item = entry({})

    def test_does_not_claim_it_will_roll_back(self):
        self.assertEqual(self.item['status'], 'unknown')
        text = self.item['what_it_does']
        self.assertIn('读不到', text)
        self.assertIn('不能确认', text)
        # 「设计上负责这件事」可以说，但不能说成「会」
        self.assertNotIn('换回上次能跑的源码版本并重新拉起', text)

    def test_no_guarantee_language(self):
        for field in ('what_it_does', 'when_it_acts', 'limits', 'what'):
            hits = promises_guarantee(str(self.item.get(field) or ''))
            self.assertEqual(hits, [], '%s 里出现了没被否定的保证性说法：%s' % (field, hits))

    def test_explicitly_tells_to_assume_worst(self):
        self.assertIn('不确定有兜底，就当没有', self.item['what_it_does'])

    def test_says_i_do_not_know(self):
        self.assertIn('不知道', self.item['what'])


class WordingNeverContradictsStatus(unittest.TestCase):
    """跨状态一致性：三种状态的措辞不得互相矛盾。"""

    def test_each_state_has_distinct_promise(self):
        ready = entry({'is-active': 'active', 'is-enabled': 'enabled'})
        dead = entry({'is-active': 'inactive', 'is-enabled': 'disabled'})
        blind = entry({})
        texts = [ready['what_it_does'], dead['what_it_does'], blind['what_it_does']]
        self.assertEqual(len(set(texts)), 3, '三种状态的描述必须各不相同')
        # 只有 ready 可以说「会换回」
        self.assertIn('换回', ready['what_it_does'])
        self.assertNotIn('并重新拉起', dead['what_it_does'])
        self.assertNotIn('并重新拉起', blind['what_it_does'])

    def test_entry_self_contained(self):
        """判据 3：不靠注释或文档，条目内容本身说清。"""
        item = entry({'is-active': 'inactive', 'is-enabled': 'disabled'})
        joined = ' '.join(str(item.get(k) or '') for k in
                          ('reason', 'what', 'what_it_does', 'when_it_acts', 'limits'))
        self.assertIn('手动', joined, '条目自己就要说出「要人手动」')
        self.assertIn('没有在做事', joined, '条目自己就要说出「现在没在做事」')


if __name__ == '__main__':
    unittest.main()
