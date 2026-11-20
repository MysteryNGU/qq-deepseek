"""P40 ②：任务清单——接现有项目记录，不另建一份待办。

**为什么不新建表**：项目记录（`meta.k = 'action_task:<ref>'`）已经在记
目标、状态、下一步、卡在哪、更新时间、依据。再建一张"待办表"就会立刻出现
两份互相矛盾的清单——用户明确禁止这个。

**这个工具真正要解决的问题**（今天 16:30–16:39 实测）：
她在 9 分钟里对同一个项目「修好APK三处体验」执行了 8 次。
根因不是「没有清单」，是**她看不见自己已经在这个目标上试过什么**。

所以 `op=add` 的语义不是「加一条」，是**「这条我做过了吗」**：
同一个 goal 再来一次，回执里必须出现 `repeat: true`、做过几次、上次什么时候、
上次结果，以及**之前留下的卡点**。她据此就能自己判断「这件事我试过了，别再重来」。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid

# 与项目记录共用的状态词。不新增状态：新增就等于造第二套语义。
STATUSES = ('todo', 'doing', 'blocked', 'done')
# 她的项目记录里已有的状态词到清单状态的映射（只读地翻译，不改源数据）。
PROJECT_STATUS = {'active': 'doing', 'waiting': 'blocked', 'paused': 'blocked',
                  'done': 'done', 'cancelled': 'done'}
OPS = ('add', 'update', 'list', 'clear')

# 同一个 goal 的判定：归一化后比对（去空白、标点、大小写）。
_STRIP = re.compile(r'[\s，。、；：,.;:!?！？"\'“”()（）\[\]【】]+')


def normalize(goal):
    return _STRIP.sub('', str(goal or '').strip().lower())


def fingerprint(goal):
    """稳定的目标指纹。用它判断「这是不是同一件事」，而不是靠字符串相等。"""
    return hashlib.sha1(normalize(goal).encode('utf-8')).hexdigest()[:16]


def _count(*values):
    """从一个字段里取"次数"。只要数字；列表/字符串/字典一律当作不知道。

    `progress` 在生产里有两种形态：整数次数，和 checkpoint 对象列表。
    后者是历史数据，不是错误，但 `int()` 它会让整个清单崩掉——一条坏字段
    不该带走其它任务。
    """
    for value in values:
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return int(value) or None
    return None


def _age(seconds):
    seconds = max(0, int(seconds))
    if seconds < 90:
        return '%d秒' % seconds
    if seconds < 5400:
        return '%d分钟' % (seconds // 60)
    if seconds < 172800:
        return '%d小时' % (seconds // 3600)
    return '%d天' % (seconds // 86400)


class TaskList:
    """项目记录的清单视图。所有读写都落在 `meta` 的 action_task 行上。"""

    def __init__(self, repo=None, agent_id=None, scope_id=''):
        self.repo = repo
        self.agent_id = agent_id or ''
        self.scope_id = scope_id or ''

    # ---- 读写投影 -----------------------------------------------------
    def _rows(self):
        if self.repo is None:
            return []
        rows = self.repo._all("SELECT k, v FROM meta WHERE k LIKE 'action_task:%' "
                              "ORDER BY rowid DESC", ())
        result = []
        for row in rows:
            try:
                value = json.loads(row['v'])
            except (TypeError, ValueError):
                continue
            if not isinstance(value, dict):
                continue
            if self.scope_id and value.get('scope_id') not in (self.scope_id, '', None):
                continue
            if value.get('ephemeral'):
                continue
            result.append(value)
        return result

    def _save(self, value):
        if self.repo is None:
            raise RuntimeError('任务清单没有接到台账上')
        with self.repo.tx():
            self.repo._exec('INSERT INTO meta(k, v) VALUES(?, ?) '
                            'ON CONFLICT(k) DO UPDATE SET v=excluded.v',
                            ('action_task:' + value['ref'], json.dumps(value, ensure_ascii=False)))
        return value

    # ---- 视图 ---------------------------------------------------------
    @staticmethod
    def _item(value, now):
        status = PROJECT_STATUS.get(str(value.get('status') or ''), 'todo')
        updated = value.get('updated_at') or value.get('last_progress_at') or 0
        blocked_on = value.get('blocked_on') or []
        if isinstance(blocked_on, str):
            blocked_on = [blocked_on]
        return dict(ref=value.get('ref'), key=value.get('key') or '',
                    goal=str(value.get('goal') or '')[:300], status=status,
                    project_status=value.get('status'), origin=value.get('origin'),
                    next_step=str(value.get('next_step') or '')[:300],
                    blocked_on=list(blocked_on)[:6],
                    stuck_for=_age(now - float(updated)) if updated else '不知道',
                    stuck_seconds=round(max(0.0, now - float(updated)), 1) if updated else None,
                    runs=_count(value.get('attempts'), value.get('progress')),
                    last_receipt=str(value.get('last_receipt') or '')[:200])

    # ---- 四个 op ------------------------------------------------------
    def add(self, goal, *, done_when='', depends_on=(), origin='self', now=None):
        now = time.time() if now is None else float(now)
        goal = str(goal or '').strip()
        if not goal:
            raise ValueError('add 需要 goal：要做的是什么。')
        stamp_key = fingerprint(goal)
        existing = [row for row in self._rows() if fingerprint(row.get('goal')) == stamp_key]
        if existing:
            # 这就是那个反复重来的场景：同一条目标又来一次。
            prior = max(existing, key=lambda row: float(row.get('updated_at') or 0))
            item = self._item(prior, now)
            # 重复不新建行（否则会变成两份待办），但把"又来了一次"记在行上，
            # 这样 list 里显示的次数是真实的尝试次数，而不是行数。
            attempts = (_count(prior.get('attempts')) or 1) + 1
            prior['attempts'] = attempts
            prior['last_attempt_at'] = now
            self._save(prior)
            receipt = dict(status='completed', op='add', repeat=True,
                           ref=prior.get('ref'), runs=attempts,
                           last_time=time.strftime('%m-%d %H:%M',
                                                   time.localtime(float(prior.get('updated_at') or 0))),
                           last_state=item['status'], last_receipt=item['last_receipt'],
                           blocked_on=item['blocked_on'], next_step=item['next_step'],
                           stuck_for=item['stuck_for'],
                           output='这条**已经有过**：%s 起共 %d 次，最近一次在 %s（%s），卡了 %s。%s'
                                  '先看上次留下的下一步与卡点，别当作新事从头再来一遍。'
                                  % (item['key'] or prior.get('ref'), attempts, receipt_time(prior),
                                     item['status'], item['stuck_for'],
                                     ('上次回执：%s。' % item['last_receipt']) if item['last_receipt'] else ''),
                           already_tried=True)
            if item['blocked_on']:
                receipt['output'] += '上次卡在：%s。' % '、'.join(item['blocked_on'])
            if item['next_step']:
                receipt['output'] += '上次写的下一步：%s。' % item['next_step']
            return receipt
        value = dict(ref='companion-project_' + uuid.uuid4().hex[:22], key=goal[:40], goal=goal,
                     status='active', origin=origin, scope_id=self.scope_id,
                     checkpoint=('做完的判据：' + str(done_when)[:300]) if done_when else '',
                     next_step='', blocked_on=[], progress=0, revision=1,
                     depends_on=[str(item)[:60] for item in (depends_on or [])][:6],
                     created_at=now, updated_at=now, fingerprint=stamp_key)
        self._save(value)
        return dict(status='completed', op='add', repeat=False, ref=value['ref'],
                    goal=goal, state='todo', created_at=now,
                    output='已记下一条：%s。同一条再提交会告诉你已经做过几次，不会再建一份。'
                           % goal[:80])

    def update(self, ref, *, status=None, blocked_on=None, next_step=None, now=None):
        now = time.time() if now is None else float(now)
        ref = str(ref or '').strip()
        rows = {row.get('ref'): row for row in self._rows()}
        value = rows.get(ref) or next(
            (row for row in rows.values()
             if ref and (row.get('key') == ref or fingerprint(row.get('goal')) == fingerprint(ref))),
            None)
        if value is None:
            raise KeyError(ref)
        if status is not None:
            status = str(status).strip().lower()
            if status not in STATUSES:
                raise ValueError('状态只能是 %s，收到 %r' % ('/'.join(STATUSES), status))
            value['status'] = {'todo': 'active', 'doing': 'active',
                               'blocked': 'waiting', 'done': 'done'}[status]
        if blocked_on is not None:
            value['blocked_on'] = [str(item)[:40] for item in
                                   (blocked_on if isinstance(blocked_on, (list, tuple))
                                    else [blocked_on]) if str(item).strip()][:6] or ([] if status == 'done' else value.get('blocked_on') or [])
        if next_step is not None:
            value['next_step'] = str(next_step)[:300]
            value['next_step_set_at'] = now
        value['updated_at'] = now
        value['progress'] = int(value.get('progress') or 0) + (1 if status in ('doing', 'done') else 0)
        value['revision'] = int(value.get('revision') or 1) + 1
        if value['status'] == 'done' and not value.get('closed_at'):
            value['closed_at'] = now
        self._save(value)
        item = self._item(value, now)
        return dict(status='completed', op='update', ref=value['ref'], state=item['status'],
                    blocked_on=item['blocked_on'], next_step=item['next_step'],
                    stuck_for=item['stuck_for'],
                    output='%s → %s%s%s' % (
                        value.get('key') or value['ref'], item['status'],
                        ('，卡在：' + '、'.join(item['blocked_on'])) if item['blocked_on'] else '',
                        ('，下一步：' + item['next_step']) if item['next_step'] else ''))

    def list(self, *, status=None, now=None, limit=40):
        now = time.time() if now is None else float(now)
        rows = self._rows()
        # 默认不列被 clear 收起来的；但显式问某个状态时必须看全——
        # 「收起来」是视图折叠，她一问就还得能拿到。
        if not status:
            rows = [row for row in rows if not row.get('hidden_at')]
        items = [self._item(row, now) for row in rows]
        if status:
            item_status = {'active': 'doing', 'waiting': 'blocked', 'paused': 'blocked',
                           'done': 'done', 'cancelled': 'done'}.get(str(status), str(status))
            items = [item for item in items if item['status'] == item_status]
        order = {'doing': 0, 'blocked': 1, 'todo': 2, 'done': 3}
        items.sort(key=lambda item: (order.get(item['status'], 9),
                                     -(item['stuck_seconds'] or 0)))
        items = items[:max(1, min(int(limit or 40), 80))]
        # 按目标指纹统计**历史上出现过几次**：同一件事做过几次，直接给出来。
        # 「做过几次」= 行数 + 每次重复 add 在该行上累加的 attempts - 1。
        # 只数行会把「重复提交过的同一件事」显示成 1 次——那正是这个工具要避免的误判。
        seen = {}
        for row in rows:
            key = fingerprint(row.get('goal'))
            seen.setdefault(key, 0)
            seen[key] += max(1, _count(row.get('attempts')) or 1)
        repeated = [(item, max(1, seen.get(fingerprint(item['goal']), 1))) for item in items]
        lines = []
        for item, runs in repeated:
            line = '%s｜%s｜%s' % (item['key'] or item['ref'], item['status'], item['goal'][:60])
            if item['blocked_on']:
                line += '｜卡在：' + '、'.join(item['blocked_on'])
            line += '｜卡了 %s' % item['stuck_for']
            if runs > 1:
                line += '｜**这件事做过 %d 次**' % runs
            lines.append(line)
        return dict(status='completed', op='list', count=len(items),
                    items=[dict(item, runs=runs) for item, runs in repeated],
                    output='\n'.join(lines) or '清单是空的。')

    def clear(self, *, now=None):
        """把已完成的条目从**默认列表**里收起来——真写标记，不是嘴上说说。

        以前这里只收集不落盘，回执却说「清单默认不列它们」，而 list() 根本没过滤，
        于是下一次 list 它们还在。这是「把没做的事说成做了」。

        现在：给这些行写 `hidden_at`，`list()` 默认跳过它们；
        **但 `list(status='done')` 仍然列得出来**——收起来不等于看不见，
        也绝不删数据。
        """
        now = time.time() if now is None else float(now)
        cleared = []
        for row in self._rows():
            if PROJECT_STATUS.get(str(row.get('status') or '')) == 'done' and not row.get('hidden_at'):
                row['hidden_at'] = now
                self._save(row)
                cleared.append(row.get('key') or row.get('ref'))
        if not cleared:
            hidden = sum(1 for row in self._rows() if row.get('hidden_at'))
            return dict(status='completed', op='clear', cleared=0, hidden_total=hidden,
                        output=('没有新收起已完成的条目。' if hidden == 0 else
                                '已完成的 %d 条早就收起来了；用 op=list 加 status="done" 仍能看全。'
                                % hidden))
        return dict(status='completed', op='clear', cleared=len(cleared), keys=cleared[:10],
                    hidden_total=sum(1 for row in self._rows() if row.get('hidden_at')),
                    output=('收起了 %d 条已完成的（%s）。数据没删，仍在项目记录里；'
                            '默认清单不再列它们，要看用 op=list 加 status="done"。'
                            % (len(cleared), '、'.join(str(k)[:20] for k in cleared[:3]))))


def receipt_time(value):
    stamp = float(value.get('updated_at') or 0)
    return time.strftime('%m-%d %H:%M', time.localtime(stamp)) if stamp else '时间不明'
