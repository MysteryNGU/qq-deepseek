"""P40 ③：结构化提问——让问题显性化，而不是再造一个提问通道。

**为什么需要**：她今天在对话里问过很多次（「您是不是在哪看到说它不能用？」），
但那只是气泡里的一句话。她随后**没有任何办法知道用户答了没有、答的是哪一句**——
下一轮她只能凭记忆猜，或者干脆再问一遍。

**这个工具解决的不是「怎么问」**（她已经在问了），是**「问完之后怎么知道」**：

* 问题带着 `id` 存下来，进入这一轮上下文，也进入下一轮的 `current_state`。
* **用户的下一条消息就是答案**——不需要前端配合，不需要用户点按钮。
  这是关键设计：她的提问和主人的回答本来就在同一条时间线上，
  另造一个"答题表单"反而会让回答脱离对话。
* 超时没答就**如实说「问过但没等到回答」**，不许当成问过了、也不许当成用户拒绝。
* 同一个问题再问一次，回 `repeat=true` 并给出已经问过几次——
  复用 ② 的思路，避免她反复问同一件事。

**不弹窗**：`ask` 只登记问题并把问题文本交回给她自己发；程序不发消息。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid

OPS = ('ask', 'answer', 'pending', 'cancel')
# 一个问题最长挂多久（秒）。默认 30 分钟：太短会把"主人晚点回"当成没问过。
DEFAULT_TTL = 1800
MAX_TTL = 86400
MAX_PENDING = 5

_STRIP = re.compile(r'[\s，。、；：,.;:!?！？"\'“”()（）\[\]【】]+')


def fingerprint(question):
    return hashlib.sha1(_STRIP.sub('', str(question or '').lower()).encode()).hexdigest()[:16]


def _age(seconds):
    seconds = max(0, int(seconds))
    if seconds < 90:
        return '%d秒' % seconds
    if seconds < 5400:
        return '%d分钟' % (seconds // 60)
    if seconds < 172800:
        return '%d小时' % (seconds // 3600)
    return '%d天' % (seconds // 86400)


class PendingQuestions:
    """挂在角色的 state 上（`core._state`），不新建表。

    存的是一条列表：每项 `{id, question, options, asked_at, expires_at, answered, repeat}`。
    用 state 而不是表，是因为它天然跟着这段关系走、也天然被快照带出去；
    问题不是长期数据，没必要为它加一张表。
    """

    KEY = 'pending_questions'

    def __init__(self, core=None, now=None):
        self.core = core
        self.now = time.time() if now is None else float(now)

    # ---- 存取 ---------------------------------------------------------
    def _load(self):
        if self.core is None:
            return []
        raw = self.core._state(self.KEY) or []
        return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []

    def _store(self, rows):
        if self.core is None:
            raise RuntimeError('提问没有接到角色上')
        self.core._save(self.KEY, rows[-MAX_PENDING * 4:])

    def _live(self, rows, now):
        return [row for row in rows if not row.get('answered')]

    # ---- op=ask -------------------------------------------------------
    def ask(self, question, *, options=(), expires_in=DEFAULT_TTL, now=None):
        now = self.now if now is None else float(now)
        question = str(question or '').strip()
        if not question:
            raise ValueError('ask 需要 question：你要问的那句话。')
        rows = self._load()
        key = fingerprint(question)
        prior = [row for row in rows if fingerprint(row.get('question')) == key]
        if prior:
            last = prior[-1]
            if not last.get('answered') and not self._expired(last, now):
                # 还挂着没答：不要重复问，把那条交回去让她接着说。
                last['repeat'] = int(last.get('repeat') or 1) + 1
                self._store(rows)
                return dict(status='completed', op='ask', id=last['id'], repeat=True,
                            asked_times=int(last.get('repeat') or 1),
                            asked_for=_age(now - float(last.get('asked_at') or now)),
                            options=last.get('options') or [],
                            output='这个问题**还挂着没答**（已经问过 %d 次，%s 起）：%s。'
                                   '不用再问一遍；可以换个说法、给个默认做法，或者就先按自己的判断做。'
                                   % (int(last.get('repeat') or 1),
                                      _age(now - float(last.get('asked_at') or now)),
                                      question[:120]))
        ttl = max(30, min(int(expires_in or DEFAULT_TTL), MAX_TTL))
        row = dict(id='ask_' + uuid.uuid4().hex[:12], question=question[:600],
                   options=[str(item)[:80] for item in (options or [])][:6] or [],
                   asked_at=now, expires_at=now + ttl, answered=False, repeat=1)
        rows.append(row)
        self._store(rows)
        return dict(status='completed', op='ask', id=row['id'], repeat=False,
                    question=row['question'], options=row['options'],
                    expires_in=ttl,
                    output='问题已登记（%s）。**主人接下来那条消息就是答案**，'
                           '不用等他说"回答"；超过 %s 没回，就当成没等到回答，'
                           '别当成他答应了或拒绝了。' % (row['id'], _age(ttl)))

    @staticmethod
    def _expired(row, now):
        return bool(row.get('answered')) or float(row.get('expires_at') or 0) <= now

    # ---- op=answer ----------------------------------------------------
    def answer(self, text=None, now=None, question_id=None):
        """认领答案：把（最近一条）未答的问题标成已答，并说明是超时认领还是当场。

        她的下一条用户消息就是答案——这里不需要用户做任何额外动作。
        """
        now = self.now if now is None else float(now)
        rows = self._load()
        live = [row for row in self._live(rows, now)]
        if question_id:
            live = [row for row in live if row.get('id') == question_id]
        if not live:
            return dict(status='completed', op='answer', matched=0,
                        output='现在没有挂着的问题，这条消息当作普通对话。')
        row = live[-1]
        expired = float(row.get('expires_at') or 0) <= now
        row['answered'] = True
        row['answered_at'] = now
        row['answer_text'] = str(text or '')[:400]
        row['answered_late'] = expired
        self._store(rows)
        return dict(status='completed', op='answer', matched=1, id=row['id'],
                    question=row.get('question'), answer=row.get('answer_text'),
                    answered_late=expired,
                    waited=_age(now - float(row.get('asked_at') or now)),
                    output=('问过「%s」，现在收到了回答（等了 %s）。'
                            % (str(row.get('question'))[:80],
                               _age(now - float(row.get('asked_at') or now))))
                           + ('这条回答是在超时之后才来的。' if expired else ''))

    # ---- op=pending ---------------------------------------------------
    def pending(self, now=None):
        now = self.now if now is None else float(now)
        rows = self._load()
        live = []
        for row in rows:
            if row.get('answered'):
                continue
            expired = float(row.get('expires_at') or 0) <= now
            live.append(dict(id=row.get('id'), question=str(row.get('question'))[:200],
                             options=row.get('options') or [],
                             asked_for=_age(now - float(row.get('asked_at') or now)),
                             expired=expired,
                             asked_times=int(row.get('repeat') or 1),
                             state='expired' if expired else 'waiting'))
        lines = []
        for item in live:
            line = '%s｜%s｜问了 %s' % (item['id'][-8:], item['question'][:60], item['asked_for'])
            if item['asked_times'] > 1:
                line += '｜问过 %d 次' % item['asked_times']
            if item['expired']:
                line += '｜**已经超过等的时间，没等到回答——这不是拒绝，是没答**'
            lines.append(line)
        return dict(status='completed', op='pending', count=len(live), items=live,
                    output='\n'.join(lines) or '现在没有挂着的问题。')

    # ---- op=cancel ----------------------------------------------------
    def cancel(self, question_id=None, now=None):
        now = self.now if now is None else float(now)
        rows = self._load()
        live = [row for row in self._live(rows, now)
                if not question_id or row.get('id') == question_id]
        if not live:
            return dict(status='completed', op='cancel', cancelled=0,
                        output='没有挂着的问题可撤。')
        row = live[-1]
        row['answered'] = True
        row['cancelled'] = True
        row['answered_at'] = now
        self._store(rows)
        return dict(status='completed', op='cancel', cancelled=1, id=row.get('id'),
                    output='撤回了这个问题（%s）。撤回不等于主人答过。' % row.get('id'))

    # ---- 给上下文投影 --------------------------------------------------
    def questions_view(self, now=None, *, limit=6):
        """`current_state.questions` 的内容——**这是 GUIDES 里承诺的那个键**。

        每条给出：question / asked_at / status / answer / answered_at。
        status 只有三个词，且互斥：
          open      —— 还挂着，等他答
          answered  —— 他已经答了，answer 是原话（可能是超时之后才答的，见 late）
          expired   —— 超过 expires_in 没答；**这不是拒绝**
        最近答过的那条一定在内：她需要知道"上一轮我问的那件事，他到底怎么答的"。
        """
        now = self.now if now is None else float(now)
        rows = self._load()
        out = []
        for row in rows[-limit * 2:]:
            answered = bool(row.get('answered'))
            expired = float(row.get('expires_at') or 0) <= now
            if answered and row.get('cancelled'):
                status = 'cancelled'
            elif answered:
                status = 'answered'
            elif expired:
                status = 'expired'
            else:
                status = 'open'
            out.append(dict(
                id=row.get('id'),
                question=str(row.get('question'))[:300],
                asked_at=row.get('asked_at'),
                asked_for=_age(now - float(row.get('asked_at') or now)),
                status=status,
                answer=row.get('answer_text') if answered else None,
                answered_at=row.get('answered_at') if answered else None,
                late=bool(row.get('answered_late')),
                expired=expired,
                asked_times=int(row.get('repeat') or 1),
                options=row.get('options') or []))
        return out[-limit:]

    def context(self, now=None):
        """旧接口，保留给老调用方；新代码用 questions_view。"""
        now = self.now if now is None else float(now)
        rows = self._load()
        live = [row for row in self._live(rows, now)]
        answered = [row for row in rows if row.get('answered')][-2:]
        return dict(pending=[dict(id=row.get('id'), question=row.get('question'),
                                  asked_for=_age(now - float(row.get('asked_at') or now)),
                                  expired=float(row.get('expires_at') or 0) <= now)
                             for row in live],
                    answered=[dict(id=row.get('id'), question=row.get('question'),
                                   answer=row.get('answer_text'),
                                   late=bool(row.get('answered_late')),
                                   cancelled=bool(row.get('cancelled')))
                              for row in answered])
