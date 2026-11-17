# -*- coding: utf-8 -*-
r"""她说过的话走到了哪一步。

状态链：queued -> sending -> sent -> rendered
                        \-> failed（明确失败）
                        \-> unknown（结果未知，不能改写成失败）

语义边界（不要混用）：
- sending  已交给发布循环，还没有结果。
- sent     服务端已生成这条消息事件并交给传输；对“她说过这句话”成立，
           但不证明用户看见了。
- rendered 端上回执确认已呈现；只证明端上已呈现，不证明用户本人看见或读过。
- unknown  发过但拿不到确认。停止自动重试，也不能当成“已送达”或“没送到”。
- failed   明确失败。
- cancelled 被插话或新表达替代，原因保留。

message_id 由 (turn_id, segment) 稳定派生，重连与重启都不会产生第二个气泡。
"""
from __future__ import annotations

import json
import logging
import time

from .text import stable_id

log = logging.getLogger(__name__)

QUEUED = 'queued'
SENDING = 'sending'
SENT = 'sent'
RENDERED = 'rendered'
FAILED = 'failed'
UNKNOWN = 'unknown'
CANCELLED = 'cancelled'

LIVE = (QUEUED, SENDING)
# 需要进入下一轮材料的状态：说出口但没确认呈现，或者干脆没送出去。
UNSETTLED = (QUEUED, SENDING, UNKNOWN, FAILED)

SCHEMA = """
CREATE TABLE IF NOT EXISTS companion_deliveries (
  message_id       TEXT PRIMARY KEY,
  agent_id         TEXT NOT NULL,
  turn_id          TEXT NOT NULL,
  segment          INTEGER NOT NULL DEFAULT 0,
  conversation_ref TEXT NOT NULL DEFAULT '',
  channels         TEXT NOT NULL DEFAULT '[]',
  text             TEXT NOT NULL DEFAULT '',
  images           TEXT NOT NULL DEFAULT '[]',
  state            TEXT NOT NULL DEFAULT 'queued',
  reason           TEXT NOT NULL DEFAULT '',
  event_ref        TEXT NOT NULL DEFAULT '',
  superseded_by    TEXT NOT NULL DEFAULT '',
  queued_at        REAL NOT NULL,
  updated_at       REAL NOT NULL,
  sent_at          REAL,
  rendered_at      REAL
);
CREATE INDEX IF NOT EXISTS ix_companion_deliveries_turn
  ON companion_deliveries(agent_id, turn_id, state);
"""


def message_id(turn_id, segment):
    """同一轮同一段永远得到同一个 id，重放天然幂等。"""
    return stable_id('companion-segment', str(turn_id), str(int(segment)))


def ensure(repo):
    with repo.tx():
        for statement in SCHEMA.strip().split(';'):
            if statement.strip():
                repo._exec(statement)
        existing = {row['name'] for row in repo._all('PRAGMA table_info(companion_deliveries)')}
        for column in ('sent_at', 'rendered_at'):
            if column not in existing:
                repo._exec('ALTER TABLE companion_deliveries ADD COLUMN %s REAL' % column)


def _row(repo, mid):
    return repo._one('SELECT * FROM companion_deliveries WHERE message_id=?', (mid,))


def state(repo, mid):
    ensure(repo)
    row = _row(repo, mid)
    return row['state'] if row else None


def _route(conversation_ref):
    """会话引用落库前先校验并规范化：裸数字或辨认不出平台的，直接拒绝。

    规范形是 ``平台:类型:对象``（qq:private:1、qq:group:1078301687、wechat:private:1、
    desktop、mobile:<id>）。空串表示「沿用默认投递」，仍然允许；反过来，认不出的
    写法不能被当成某个平台存进台账——那样以后按目标追溯就会认错人。
    """
    ref = str(conversation_ref or '').strip()
    if not ref or ref.lower() in ('all',):
        # 空串 = 沿用默认投递；all = 同时提醒所有端，二者都不是某一具体会话。
        return ref.lower()
    try:
        from .companion_agents import normalize_route
    except ImportError:  # 精简检出里没有子 agent 模块：只做最小校验
        if ref.isdigit():
            raise ValueError('conversation_ref_ambiguous: 裸数字不是会话引用')
        return ref
    route = normalize_route(conversation_ref=ref)
    return route['conversation_ref']


def queue(repo, *, mid, agent_id, turn_id, segment, text, images=(), conversation_ref='', channels=(), now=None):
    """登记一次发送意图。已存在的同一 id 不会被再写一遍。"""
    conversation_ref = _route(conversation_ref)
    at = time.time() if now is None else float(now)
    ensure(repo)
    if _row(repo, mid):
        return dict(_row(repo, mid))
    with repo.tx():
        repo._exec(
            'INSERT OR IGNORE INTO companion_deliveries'
            '(message_id,agent_id,turn_id,segment,conversation_ref,channels,text,images,state,queued_at,updated_at)'
            ' VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (mid, agent_id, turn_id, int(segment), conversation_ref,
             json.dumps(list(channels), ensure_ascii=False),
             text or '', json.dumps(list(images or []), ensure_ascii=False),
             QUEUED, at, at))
    return dict(_row(repo, mid))


def _transition(repo, mid, state, *, reason='', event_ref=None, now=None, allowed=None, stamp=None):
    at = time.time() if now is None else float(now)
    with repo.tx():
        row = _row(repo, mid)
        if not row:
            return None
        if allowed is not None and row['state'] not in allowed:
            return dict(row)
        fields = ['state=?', 'updated_at=?']
        params = [state, at]
        if reason:
            fields.append('reason=?')
            params.append(reason)
        if event_ref is not None:
            fields.append('event_ref=?')
            params.append(event_ref)
        if stamp:
            fields.append('%s=?' % stamp)
            params.append(at)
        if state in (SENT, RENDERED):
            # 正文已由事件记录保存，账本不再留第二份副本。
            fields.append('text=?')
            params.append('')
        params.append(mid)
        repo._exec('UPDATE companion_deliveries SET %s WHERE message_id=?' % ','.join(fields), tuple(params))
    return dict(_row(repo, mid))


def mark_sending(repo, mid, now=None):
    return _transition(repo, mid, SENDING, now=now, allowed=LIVE)


def mark_sent(repo, mid, event_ref='', now=None):
    """服务端确实生成了这条消息事件并交给传输。不等于用户已看见。"""
    return _transition(repo, mid, SENT, event_ref=event_ref, now=now, stamp='sent_at',
                       allowed=(QUEUED, SENDING, UNKNOWN))


def confirm_render(repo, mid, now=None):
    """端上回执：只证明端上已呈现，不证明用户已读。迟到的回执仍然有效。"""
    return _transition(repo, mid, RENDERED, now=now, stamp='rendered_at',
                       allowed=(QUEUED, SENDING, SENT, UNKNOWN))


def acknowledge_events(repo, *, agent_id, event_ids, channel, now=None):
    """An authenticated client confirms DOM presentation, never human reading."""
    ensure(repo)
    confirmed = []
    for event_id in dict.fromkeys(event_ids[:100]):
        if not isinstance(event_id, str):
            continue
        rows = repo._all('SELECT message_id,channels,state FROM companion_deliveries '
                         'WHERE agent_id=? AND event_ref=?', (agent_id, event_id))
        for row in rows:
            targets = json.loads(row['channels'])
            if targets and 'all' not in targets and channel not in targets:
                continue
            if row['state'] not in (SENT, UNKNOWN, RENDERED):
                continue
            if row['state'] != RENDERED:
                confirm_render(repo, row['message_id'], now=now)
            confirmed.append(event_id)
    return list(dict.fromkeys(confirmed))


def fail(repo, mid, reason='', now=None):
    return _transition(repo, mid, FAILED, reason=reason, now=now, allowed=LIVE)


def mark_unknown(repo, mid, reason='', now=None):
    return _transition(repo, mid, UNKNOWN, reason=reason, now=now, allowed=LIVE)


def cancel(repo, mid, reason='', superseded_by='', now=None):
    row = _transition(repo, mid, CANCELLED, reason=reason, now=now, allowed=LIVE + (UNKNOWN,))
    if row and superseded_by:
        with repo.tx():
            repo._exec('UPDATE companion_deliveries SET superseded_by=? WHERE message_id=?', (superseded_by, mid))
    return row


def cancel_turn(repo, agent_id, turn_id, reason='', now=None):
    """用户插话后取消该轮尚未发出的段落，原因保留。"""
    ensure(repo)
    ids = [r['message_id'] for r in repo._all(
        "SELECT message_id FROM companion_deliveries WHERE agent_id=? AND turn_id=? AND state IN ('queued','sending')",
        (agent_id, turn_id))]
    return [cancel(repo, mid, reason=reason, now=now) for mid in ids]


def reconcile(repo, *, timeout=180.0, now=None, reason='核对期限结束仍未取得端上回执'):
    """发送中超过期限的段落进入未知：停止自动重试，但不改写成失败。"""
    at = time.time() if now is None else float(now)
    ensure(repo)
    rows = repo._all("SELECT message_id FROM companion_deliveries "
                     "WHERE state IN ('queued','sending') AND updated_at<=?", (at - float(timeout),))
    out = []
    for row in rows:
        updated = mark_unknown(repo, row['message_id'], reason=reason, now=at)
        if updated:
            out.append(updated)
    return out


def reconcile_on_start(repo, *, agent_id, now=None):
    """进程重启时，上次停在发送中的段落结果不可知，一律降为 unknown。"""
    at = time.time() if now is None else float(now)
    ensure(repo)
    rows = repo._all("SELECT message_id FROM companion_deliveries WHERE agent_id=? AND state='sending'",
                     (agent_id,))
    return [mark_unknown(repo, row['message_id'], reason='进程重启，发送结果未知', now=at) for row in rows]


def expire_unknown(repo, *, agent_id, after=3600.0, now=None):
    """未知段落保留一段时间供她判断，超时后不再反复进入材料（记录仍保留）。"""
    at = time.time() if now is None else float(now)
    with repo.tx():
        repo._exec("UPDATE companion_deliveries SET superseded_by='expired',updated_at=? "
                   "WHERE agent_id=? AND state='unknown' AND superseded_by='' AND updated_at<=?",
                   (at, agent_id, at - float(after)))
    return None


def pending(repo, *, agent_id, now=None, limit=5):
    """进入下一轮材料的未确认表达；已呈现、已取消的不再补发。"""
    ensure(repo)
    rows = repo._all(
        "SELECT * FROM companion_deliveries WHERE agent_id=? AND state IN ('queued','sending','unknown','failed')"
        " AND superseded_by='' ORDER BY queued_at ASC LIMIT ?", (agent_id, int(limit)))
    return [dict(row) for row in rows]


def recent(repo, *, agent_id, limit=20):
    ensure(repo)
    rows = repo._all('SELECT * FROM companion_deliveries WHERE agent_id=? ORDER BY queued_at DESC LIMIT ?',
                     (agent_id, int(limit)))
    return [dict(row) for row in rows]


def summary(repo, *, agent_id, turn_id=None):
    ensure(repo)
    clause = 'AND turn_id=?' if turn_id else ''
    params = (agent_id, turn_id) if turn_id else (agent_id,)
    rows = repo._all('SELECT state, COUNT(*) AS n FROM companion_deliveries '
                     'WHERE agent_id=? %s GROUP BY state' % clause, params)
    counts = {row['state']: int(row['n']) for row in rows}
    return dict(
        counts=counts,
        sent=int(counts.get(SENT, 0)) + int(counts.get(RENDERED, 0)),
        rendered=int(counts.get(RENDERED, 0)),
        undelivered=int(counts.get(FAILED, 0)),
        unknown=int(counts.get(UNKNOWN, 0)),
        meaning='sent=服务端已生成并交给传输；rendered=端上已呈现；unknown=结果未知，不等于没送到',
    )
