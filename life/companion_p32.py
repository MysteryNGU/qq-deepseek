# -*- coding: utf-8 -*-
"""P32：插话感知与气泡收回（纯函数，独立文件，不与 companion.py 抢写）。

为什么单独成文件：2026-11-18 夜里 `life/companion.py` 被多人反复整文件重写，
同一文件上的改动互相覆盖了三次。把**能独立的部分**放进自己的文件，
`companion.py` 里只留一次极小的接线，冲突面从 5 处降到 2 行。

两条需求：
1. **插话感知**——她在回答时用户又说了话，下一轮让她知道，且**不打断当前模型调用**。
2. **气泡收回**——模型自己决定「剩下没发的气泡不要了」，只收气泡、不取消模型调用。

两者都必须留痕：插话消费后清空；收回必须记审计（取消了哪几条）。
"""
from __future__ import annotations

import time

# 插话这一段的预算上限。主上下文已经很长，这一段必须小。
INTERJECTION_MAX_CHARS = 600
INTERJECTION_MAX_ROWS = 5
INTERJECTION_ROW_CHARS = 40
# 超过这个岁数还没被消费，就说成「较早的插话」，不让她以为刚刚发生。
INTERJECTION_STALE_SECONDS = 300


def _clean(text, limit):
    return ' '.join(str(text or '').split())[:limit]


def interjection_view(rows, *, now=None):
    """把「她在回答时用户又说了」渲染成一小段上下文。

    ``rows`` 是 ``[{text, at, source}]``，按发生顺序。返回 ``dict`` 或 ``None``。
    **本函数不清空任何状态**——清空由调用方做（它持有那份列表）。

    设计取舍（用户批准的）：
    - 逐条给原文（截断），因为要知道用户说了什么，只看条数没有用；
    - 最多 5 条，其余只给条数——超预算时先砍条数；
    - 超过 5 分钟未消费，明说「较早的插话」，避免把过期状态按原样呈现。
    """
    rows = [row for row in (rows or []) if isinstance(row, dict) and str(row.get('text') or '').strip()]
    if not rows:
        return None
    now = time.time() if now is None else float(now)
    shown = rows[:INTERJECTION_MAX_ROWS]
    lines = []
    for row in shown:
        age = max(0, int(now - float(row.get('at') or now)))
        when = '刚刚' if age < 60 else ('%d 分钟前' % (age // 60))
        lines.append('%s（%s）' % (_clean(row.get('text'), INTERJECTION_ROW_CHARS), when))
    stale = (now - float(rows[0].get('at') or now)) >= INTERJECTION_STALE_SECONDS
    header = ('你说话时主人又说了几句，这些是较早的插话（不是刚发生的）：'
              if stale else '你刚才说话时主人又说了：')
    body = header + '；'.join(lines)
    if len(rows) > len(shown):
        body += '；另有 %d 条' % (len(rows) - len(shown))
    return dict(text=body[:INTERJECTION_MAX_CHARS], count=len(rows), stale=stale,
                observed_at=float(now), source_ref='life/companion_p32.py#interjection_view')


def should_record_interjection(*, answering, text, source_kind):
    """这条消息该不该记成插话。

    只在「她正在回答」时记，且排除内部来源（内部事件不是用户在说话）。
    记下来**不影响**当前这一轮的处理：那条消息本来就会在下一轮被正常回答，
    这里补的只是「她当时被打断过」这个事实。
    """
    return bool(answering) and bool(str(text or '').strip()) and source_kind != 'internal'


def decide_bubbles(bubbles, *, silence_pending=False):
    """按 ``silence_pending`` 切分这一轮的气泡。

    返回 ``(keep, dropped, silenced)``：
    - 没要求收回，或只有一条：全部保留（只有一条时"闭嘴"等于整轮不说话，是另一件事）；
    - 要求收回且多于一条：**只保留第一条**，其余不登记发送意图。
    """
    bubbles = [text for text in (bubbles or []) if str(text).strip()]
    silenced = bool(silence_pending) and len(bubbles) > 1
    keep = 1 if silenced else len(bubbles)
    dropped = [dict(part=index, text=text)
               for index, text in enumerate(bubbles[keep:], start=keep)]
    return bubbles[:keep], dropped, silenced


def silence_audit(dropped, *, kept):
    """收回气泡时的审计载荷。**不许静默吞掉**：取消了哪几条要留痕。"""
    return dict(kind='activity', text='她收回了还没发出的后续气泡。',
                details=dict(reason='silence_pending', kept=kept, dropped=len(dropped),
                             dropped_preview=[str(row.get('text') or '')[:40] for row in dropped[:5]]))
