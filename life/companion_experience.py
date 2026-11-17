# -*- coding: utf-8 -*-
"""经历：从真实行动回执提炼「这项能力我用过没有、上次怎么样」。

回答的是「**我用过什么、结果如何**」，不是「我能做什么」（能力目录在
``companion_awareness.capabilities``，用法说明在 ``companion_context.GUIDES``）。

四条硬规矩：

1. **只从台账提炼，不另建库**。唯一事实源是 ``companion_actions``（由
   ``Companion._settle`` 写入，见 ``life/companion.py``）。本模块只读，不写任何表。
2. **来源必须可回查**。每条经历都带 ``action_id``；读原文用 ``memory op=read``
   加同一个 id，取的是当时保存的回执，不是复述。
3. **亲历与听说分开**。``basis`` 只取 ``first_hand`` / ``reported`` / ``inferred``：
   - ``first_hand``：这一次动作由本机执行器完成并当场拿到回执；
   - ``reported``：结果来自子 agent / 远端 / 别的进程的自述，我们只看到它的报告；
   - ``inferred``：事实不是这一条回执给的，是从别处推出来的。
   顶层还有 ``basis_kinds`` 说明这批条目的构成——**不能把``reported``说成亲眼看过**。
4. **一次失败不是永久结论**。``standing`` 只看**最近一次**真实结果：

   - ``used_ok``：最近一次拿到成功回执；
   - ``used_failed``：最近一次失败，但**同一时间窗内**以前成功过——临时失败；
   - ``failed_in_window``：这个时间窗内没有成功过，最近一次失败（窗口窄时不等于能力不行）；
   - ``never_used``：台账里没有一条这个能力的动作。

   ``stale`` / ``environmental`` 只用来提示「上次失败可能是外部原因」，
   **不改变 standing**，也不构成「不能用」的结论。

长时间没有动作的条目不进主上下文的摘要（只进按需读取），避免旧经历挤占本轮材料。
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

from .companion_evidence import ACTION_LINK, READ, EXPLICIT_REF
from .text import stable_id

BEIJING = timezone(timedelta(hours=8))

# 主上下文摘要里最多带几条经历；细节走 memory op=experience。
SUMMARY_LIMIT = 6
# 超过这个天数的条目不进摘要（仍可在按需读取里查到）——旧经历不该挤占本轮材料。
SUMMARY_MAX_AGE_DAYS = 7

FIRST_HAND = 'first_hand'
REPORTED = 'reported'
INFERRED = 'inferred'
BASIS_KINDS = (FIRST_HAND, REPORTED, INFERRED)

# 这些 kind 的结果不是本机自己执行出来的：看到的是对方的报告，不是亲眼所见。
REPORTED_KINDS = {'dsh', 'agent'}
# 这些动作天然依赖外部进程/桥接，失败多半是环境当时不在，而不是这条能力不行。
ENVIRONMENTAL_KINDS = {'phone', 'computer', 'body', 'town', 'bilibili'}
# 「这次没成」的原因属于环境（没连上、没登录、被关掉、没跑完），而不是这条能力不行。
# 只用来提示「别把它当能力结论」，不改变 standing。
ENVIRONMENTAL_HINTS = ('未连接', '没有连接', '连接已断开', '连不上', '没连上',
                       '没有启用', '未启用', '被关掉', '关闭了', '没有登录', '未登录',
                       'login_required', 'offline', 'disconnected', 'unavailable',
                       '被停止', '没有活动连接', '未重放')
# 子 agent 台账的状态与动作台账不同名，统一成同一种说法再用。
_SUCCESS_STATES = {'completed', 'succeeded'}


def _stamp(value):
    """ISO8601 秒级 +08:00；值不是时间就返回 None（不编造）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return datetime.fromtimestamp(float(value), BEIJING).isoformat(timespec='seconds')


def _kind_of(action):
    kind = action.get('kind') if isinstance(action, dict) else None
    return str(kind or '').strip() or 'unknown'


def _basis_for(kind):
    return REPORTED if kind in REPORTED_KINDS else FIRST_HAND


def _artifacts(action, result):
    """产物位置：优先回执里写的路径，其次这条动作自己的参数。"""
    found = []
    for key in ('output_path', 'source_path', 'image_path', 'artifact_path'):
        value = result.get(key) if isinstance(result, dict) else None
        if isinstance(value, str) and value.strip():
            found.append(dict(key=key, path=value.strip()))
    for key in ('cwd', 'path'):
        value = action.get(key) if isinstance(action, dict) else None
        if isinstance(value, str) and value.strip():
            found.append(dict(key='argument.' + key, path=value.strip()))
    for item in (result.get('artifacts') or []) if isinstance(result, dict) else []:
        if isinstance(item, dict) and isinstance(item.get('path'), str) and item['path'].strip():
            found.append(dict(key='artifact', path=item['path'].strip()))
    seen, unique = set(), []
    for row in found:
        if row['path'] in seen:
            continue
        seen.add(row['path'])
        unique.append(row)
    return unique[:4]


def _what(action):
    """这次动作到底做了什么——只取模型自己写下的目标/命令/操作，不做解释。"""
    if not isinstance(action, dict):
        return ''
    for key in ('goal', 'command', 'op', 'operation'):
        value = action.get(key)
        if isinstance(value, str) and value.strip():
            return ' '.join(value.split())[:120]
    return ''


def _failure_code(result):
    """失败的机器可读原因；agent 通道的 code 常是异常类名，读起来要带上下文。"""
    if not isinstance(result, dict):
        return None
    code = result.get('code')
    if isinstance(code, str) and code.strip():
        return code.strip()[:60]
    if result.get('return_code') not in (None, 0):
        return 'exit_%s' % result.get('return_code')
    return None


def _failure_reason(result, limit=180):
    """失败原文（保留对方说的原因），不是我们替它总结的。"""
    if not isinstance(result, dict):
        return ''
    for key in ('reason', 'output', 'error', 'message'):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return ' '.join(value.split())[:limit]
    return ''


def _episode(row, data, kind):
    """一条经历：这个动作当时做了什么、结果如何、依据在哪。"""
    status = str(row['status'] or '')
    ok = status in _SUCCESS_STATES
    action = data.get('action') or {}
    result = data.get('result') or {}
    at = row['updated'] or row['created']
    episode = dict(
        action_id=row['id'], kind=kind, at=_stamp(at), at_ts=at,
        status=status, outcome='ok' if ok else 'failed',
        basis=_basis_for(kind), what=_what(action),
        project=row['project_ref'] or action.get('project'),
        artifacts=_artifacts(action, result),
        # 回查入口：同一个 id 走 memory op=read 能拿到当时的原文。
        receipt_ref='companion_actions:' + str(row['id']),
        read_op=dict(kind='memory', op='read', arguments=json.dumps(
            dict(op='read', id=row['id']), ensure_ascii=False)),
    )
    if not ok:
        episode['code'] = _failure_code(result)
        episode['reason'] = _failure_reason(result)
        # 失败原因属于环境（没连上/没登录/被关掉/没跑完）时要标出来：
        # 这种失败不能读成「这个能力不行」。
        text = ' '.join(str(episode.get('code') or '') + ' ' + str(episode.get('reason') or '')).lower()
        episode['environmental'] = (kind in ENVIRONMENTAL_KINDS
                                    or status == 'interrupted'
                                    or any(hint.lower() in text for hint in ENVIRONMENTAL_HINTS))
    return episode


def _receipt_action_ids(repo, action_ids, limit=50):
    """按动作 id 取当时落下的 tool_result 事件，作为可解析的第二重来源引用。"""
    ids = [str(item) for item in action_ids if item][:limit]
    if not ids:
        return {}
    marks = ','.join('?' for _ in ids)
    try:
        rows = repo._all(
            "SELECT id,payload FROM events WHERE kind='tool_result' AND redacted_at IS NULL "
            "AND json_extract(payload,'$.action_id') IN (" + marks + ") ORDER BY received_at DESC",
            tuple(ids))
    except Exception:  # noqa: BLE001 - 事件表读不到时只是少一重引用，不影响主结论
        return {}
    found = {}
    for row in rows or []:
        try:
            payload = json.loads(row['payload'] or '{}')
        except (TypeError, ValueError):
            continue
        action_id = payload.get('action_id')
        if action_id and action_id not in found:
            found[action_id] = row['id']
    return found


def _standing(episodes, counts):
    """只看最近一次真实结果：一次失败不等于这项能力不能用。

    注意 ``counts`` 是**同一时间窗内**的计数。窗口很窄时（比如只看今天），
    最近一次失败可能只是「今天还没成功过」，不能据此说这项能力出过问题——
    那种情况单独标 ``failed_in_window``，把「整个台账里有没有成功过」交给
    按需读取（``by_kind`` 不带 since）回答。
    """
    if not episodes:
        return dict(state='never_used',
                    note='台账里没有这个能力的动作；「没做过」不等于「做不到」。')
    latest = episodes[0]
    if latest['outcome'] == 'ok':
        return dict(state='used_ok', last_ok_at=latest['at'],
                    note='最近一次拿到了成功回执。')
    if counts.get('ok'):
        return dict(state='used_failed', last_ok_at=next(
                        (item['at'] for item in episodes if item['outcome'] == 'ok'), None),
                    note='最近一次失败，但以前成功过——这是一次临时失败，不等于这个能力不能用；'
                         '要判断现在能不能用，看 awareness 里的实时状态。')
    return dict(state='failed_in_window', last_ok_at=None,
                note='这个时间窗内没有成功记录，最近一次是失败。'
                     '窗口内没成功不等于这项能力不能用：去掉时间窗再查一次，'
                     '看整个台账里有没有成功过。现在能不能用，看 awareness 的实时状态。')


def _counts(rows):
    total = len(rows)
    passed = sum(1 for row in rows if str(row['status'] or '') in _SUCCESS_STATES)
    return dict(total=total, ok=passed, failed=total - passed)


def _reported_by_other(repo, agent_id, project_refs):
    """子 agent / 别的进程在这几件事上留下了什么——它们是「听说」，要分开标。"""
    refs = [str(item) for item in project_refs if item][:8]
    if not refs:
        return []
    marks = ','.join('?' for _ in refs)
    rows = []
    try:
        rows = repo._all(
            'SELECT id,project_ref,status,verification,updated FROM companion_agent_tasks '
            'WHERE agent=? AND project_ref IN (' + marks + ') ORDER BY updated DESC LIMIT 8',
            (agent_id, *refs))
    except Exception:  # noqa: BLE001 - 台账还没建好就当没有
        rows = []
    out = []
    for row in rows:
        out.append(dict(task_ref=row['id'], project_ref=row['project_ref'],
                        status=row['status'], verification=row['verification'],
                        at=_stamp(row['updated']), basis=REPORTED,
                        note='这是子 agent 台账里它自己的说法；completed 只表示它说做完了，'
                             '不等于核验通过。'))
    return out


class Experience:
    """只读的经历归纳；按 kind 聚合成「用过没有、上次怎么样」。"""

    def __init__(self, repo, agent_id):
        self.repo, self.agent_id = repo, agent_id

    def _rows(self, *, kind=None, since=None, until=None, limit=200):
        """按 kind 与时间窗取台账行。时间窗用的是台账真实的 created/updated。

        ``kind`` 支持单个，也支持一个列表（A2 那种「这几项用过没有」的问法）。
        """
        sql = ['SELECT id,project_ref,source,action,status,result,created,updated '
               'FROM companion_actions WHERE agent=?']
        args = [self.agent_id]
        wanted = [str(item) for item in kind] if isinstance(kind, (list, tuple, set)) else None
        if wanted:
            sql.append("AND COALESCE(json_extract(action,'$.kind'),'') IN (%s)"
                       % ','.join('?' for _ in wanted))
            args.extend(wanted)
        elif kind:
            sql.append("AND COALESCE(json_extract(action,'$.kind'),'')=?")
            args.append(str(kind))
        if since is not None:
            sql.append('AND COALESCE(updated,created)>=?')
            args.append(float(since))
        if until is not None:
            sql.append('AND COALESCE(updated,created)<=?')
            args.append(float(until))
        sql.append('ORDER BY COALESCE(updated,created) DESC LIMIT ?')
        args.append(max(1, min(int(limit), 500)))
        return list(self.repo._all(' '.join(sql), tuple(args)) or [])

    @staticmethod
    def _decode(row):
        try:
            action = json.loads(row['action'] or '{}')
        except (TypeError, ValueError):
            action = {}
        try:
            result = json.loads(row['result'] or '{}')
        except (TypeError, ValueError):
            result = {}
        return dict(action=action if isinstance(action, dict) else {},
                    result=result if isinstance(result, dict) else {})

    def _episodes(self, rows):
        out = []
        received = _receipt_action_ids(self.repo, [row['id'] for row in rows])
        for row in rows:
            data = self._decode(row)
            kind = _kind_of(data['action'])
            episode = _episode(row, data, kind)
            event_id = received.get(row['id'])
            if event_id:
                episode['tool_result_ref'] = 'events:' + event_id
                # 行动关联证据：这条动作确实动过手，并且拿到了回执。
                episode['evidence_kind'] = ACTION_LINK
            out.append(episode)
        return out

    def by_kind(self, *, kinds=None, since=None, until=None, limit=200):
        """每种动作的「用过没有、上次怎么样」。``kinds`` 是「这几项分别用过没有」的限定集。

        给了 ``kinds`` 时**每一项都会有一条**：台账里没有的给 ``never_used``，
        不能因为查不到就从列表里消失（消失会被读成「这事不存在」）。
        """
        wanted = [str(item) for item in (kinds or []) if item]
        rows = self._rows(kind=wanted or None, since=since, until=until, limit=limit)
        grouped = {}
        for row in rows:
            data = self._decode(row)
            grouped.setdefault(_kind_of(data['action']), []).append(row)
        for kind in wanted:
            grouped.setdefault(kind, [])
        episodes_by_kind = {kind: self._episodes(items) for kind, items in grouped.items()}
        catalogue = []
        for kind in sorted(grouped):
            episodes = episodes_by_kind.get(kind) or []
            counts = _counts(grouped.get(kind) or [])
            catalogue.append(dict(
                kind=kind, basis=_basis_for(kind),
                counts=counts,
                standing=_standing(episodes, counts),
                last=episodes[0] if episodes else None,
                episodes=episodes[:3],
            ))
        # 「听说」的部分单独列，不混进上面按 kind 的统计里。
        projects = [row['project_ref'] for row in rows if row['project_ref']]
        reported = _reported_by_other(self.repo, self.agent_id, projects)
        return dict(
            observed_at=_stamp(time.time()),
            window=dict(since=_stamp(since), until=_stamp(until)),
            kinds=catalogue, reported=reported,
            scope='只从 companion_actions 台账与 events 的 tool_result 提炼；'
                  '每条都带 action_id，用 memory op=read 能取回当时保存的原文。'
                  'standing 只看最近一次结果，一次失败不构成永久结论。')

    def summary(self, kinds, *, now=None, limit=SUMMARY_LIMIT):
        """主上下文用的极短摘要：只放最近成功过/最近失败的那些，且要够新。

        旧经历不进来——它仍可用 memory op=used 查到，但本轮不该为它腾地方。
        """
        now = time.time() if now is None else float(now)
        rows = self._rows(limit=120)
        cutoff = now - SUMMARY_MAX_AGE_DAYS * 86400
        latest = {}
        for row in rows:
            data = self._decode(row)
            kind = _kind_of(data['action'])
            if kinds and kind not in kinds:
                continue
            if kind in latest:
                continue
            at = row['updated'] or row['created']
            if not isinstance(at, (int, float)) or at < cutoff:
                continue
            latest[kind] = (row, data)
        items = []
        for kind, (row, data) in sorted(latest.items(),
                                        key=lambda kv: -(kv[1][0]['updated'] or kv[1][0]['created'] or 0)):
            items.append(_episode(row, data, kind))
        items = items[:limit]
        received = _receipt_action_ids(self.repo, [item['action_id'] for item in items])
        for item in items:
            event_id = received.get(item['action_id'])
            if event_id:
                item['tool_result_ref'] = 'events:' + event_id
            item['reason'] = (item.get('reason') or '')[:80]
        basis_kinds = sorted({item['basis'] for item in items})
        return dict(
            items=items,
            basis_kinds=basis_kinds,
            note=('这是最近 %d 天里每种能力最近一次的真实结果，只从动作台账来；'
                  '「没列出」只代表最近没用过，不代表做不到。要更早的用 memory op=used '
                  '加 kind/since 查，原文用 op=read 加 action_id。' % SUMMARY_MAX_AGE_DAYS),
            meaning='亲历与听说分开标：first_hand 是本机执行并当场拿到回执，'
                    'reported 是子 agent/远端自己的说法。')


def experience_key(action_id):
    """经历条目的稳定键：同一个动作永远得到同一个键，便于被引用。"""
    return stable_id('experience', str(action_id))


__all__ = ['Experience', 'experience_key', 'BASIS_KINDS', 'FIRST_HAND', 'REPORTED',
           'INFERRED', 'SUMMARY_LIMIT', 'SUMMARY_MAX_AGE_DAYS',
           'EXPLICIT_REF', 'READ', 'ACTION_LINK']
