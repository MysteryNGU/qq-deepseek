# -*- coding: utf-8 -*-
"""current_state：带时间的只读工作投影。

它回答四个问题：我现在是什么状态、我刚做过什么、手上还有什么、什么值得关注。
每个字段都从既有 state / drives / tasks / 投递账本读出来；缺就是未知（None），
不补造，也不另存一份会与真相分叉的快照。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

BEIJING = timezone(timedelta(hours=8))
PROJECTION_VERSION = 'presence-1'

# 这些是"角色自己发生的事"，必须来自明确的状态事件。
# 进程重启、模型请求、用户刚睡醒都不写进这里。
SLEEP_KEYS = ('last_sleep_at', 'last_wake_at')


def _stamp(value):
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    return datetime.fromtimestamp(float(value), BEIJING).strftime('%Y-%m-%d %H:%M')


def _ago(value, now):
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    delta = max(0.0, float(now) - float(value))
    if delta < 90:
        return '刚刚'
    if delta < 3600:
        return '%d分钟前' % int(delta // 60)
    if delta < 86400:
        return '%d小时前' % int(delta // 3600)
    return '%d天前' % int(delta // 86400)


def _section(name, errors, fn, default=None):
    """单段失败只让这一段未知，并留下可定位的记录，不吞掉整份投影。"""
    try:
        return fn()
    except Exception as error:  # noqa: BLE001 - 记录后继续，错误不静默
        errors.append('%s:%s' % (name, type(error).__name__))
        log.warning('current_state 段 %s 失败', name, exc_info=True)
        return default


def _body(core, now):
    mood = core.life.mood(core.scope)
    return dict(
        energy=round(float(mood.energy), 3) if mood else None,
        activation=round(float(mood.activation), 3) if mood else None,
        awake=core._state('awake_state'),
        updated_at=_stamp(getattr(mood, 'updated_at', None)),
        updated_ago=_ago(getattr(mood, 'updated_at', None), now),
    )


def _affect(core, now):
    summary = core.life.affect_summary(core.scope) or {}
    intensity = summary.get('intensity') or {}
    top = sorted(((k, v) for k, v in intensity.items() if isinstance(v, (int, float)) and v > 0.05),
                 key=lambda kv: -kv[1])[:3]
    causes = []
    for cause in (summary.get('causes') or [])[-2:]:
        text = str(cause.get('summary') or cause.get('reason') or '').strip()
        if text:
            causes.append(dict(text=text[:120], at=_stamp(cause.get('at'))))
    return dict(feelings=[dict(name=k, level=round(float(v), 2)) for k, v in top],
                from_what=causes, updated_at=_stamp(summary.get('updated_at')))


def _mood(core, now):
    mood = core.life.mood(core.scope)
    if not mood:
        return None
    return dict(pleasure=round(float(mood.pleasure), 3),
                activation=round(float(mood.activation), 3),
                updated_at=_stamp(getattr(mood, 'updated_at', None)),
                updated_ago=_ago(getattr(mood, 'updated_at', None), now))


def _motivation(core, attention=None):
    packet = core.motivation.context(attention=attention) or {}
    drives = packet.get('drives') or {}
    ordered = sorted(drives.items(), key=lambda kv: -(kv[1][0] if kv[1] else 0))
    strongest = [dict(drive=dim, pressure=value[0], phase=value[1] if len(value) > 1 else None)
                 for dim, value in ordered[:3] if value]
    return dict(strongest=strongest,
                recent_choices=packet.get('recent_choices') or [],
                attention=packet.get('attention') or [],
                intimacy=packet.get('intimacy') or None)


def _current_activity(core, now):
    rows = []
    for project in core._projects():
        status = project.get('status')
        if status not in {'active', 'waiting', 'paused'}:
            continue
        continuation = project.get('continuation') or {}
        rows.append(dict(
            ref=project.get('ref') or project.get('key'),
            goal=project.get('goal'),
            status=status,
            origin=project.get('origin'),
            next_step=project.get('next_step') or project.get('checkpoint') or '',
            waiting_for=(project.get('commitment') or {}).get('waiting_for') or continuation.get('reason') or '',
            recover_when=(project.get('commitment') or {}).get('recover_when') or '',
            updated_at=_stamp(project.get('updated_at')),
            updated_ago=_ago(project.get('updated_at'), now),
        ))
    rows.sort(key=lambda row: -(row['updated_at'] is not None))
    return rows[:4]


def _commitments(core, now):
    """明确约定：有持久来源的承诺，不是关键词命中出来的任务。"""
    out = []
    for project in core._projects():
        promise = project.get('commitment')
        if not isinstance(promise, dict):
            continue
        if promise.get('fulfilled_at') or project.get('status') in {'done', 'cancelled'}:
            continue
        out.append(dict(
            ref=project.get('ref') or project.get('key'),
            promise=str(promise.get('promise') or project.get('goal') or '')[:160],
            promised_to=promise.get('conversation_ref') or '',
            source_ref=promise.get('source_ref') or '',
            made_at=_stamp(promise.get('made_at')),
            made_ago=_ago(promise.get('made_at'), now),
            status=project.get('status'),
            next_step=project.get('next_step') or promise.get('next_step') or '',
            review_at=_stamp(promise.get('review_at')),
            waiting_for=promise.get('waiting_for') or '',
        ))
    return out


def _pending_expression(core, now):
    """已提交、尚未确认送达的段落。草稿不算。"""
    from .companion_delivery import pending as pending_deliveries
    rows = pending_deliveries(core.repo, agent_id=core.life.agent_id, now=now) or []
    views = []
    for row in rows:
        views.append(dict(message_id=row.get('message_id'), state=row.get('state'),
                          text=(row.get('text') or '')[:200],
                          reason=row.get('reason') or '',
                          queued_at=_stamp(row.get('queued_at')),
                          queued_ago=_ago(row.get('queued_at'), now)))
    return views


def _attention(core, now):
    next_wake = core._state('next_wake')
    return dict(chosen_next=_stamp(next_wake),
                chosen_next_in=_ago(next_wake, now) if isinstance(next_wake, (int, float)) else None,
                last_timer_wake=core._state('last_timer_wake'))


def _presence(core):
    desktop = core._body_context or {}
    phone = {}
    try:
        if hasattr(core.executor, 'phone_context'):
            phone = core.executor.phone_context() or {}
    except Exception:
        phone = {}
    # 配对过 ≠ 现在在线。基线里她自己圆出了这一点（「那是配对过的存档，不是真连着」），
    # 但那是猜出来的——这里把两件事分开给，各自带自己的时间。
    paired = []
    try:
        mobile = getattr(getattr(getattr(core, 'executor', None), 'server', None), 'mobile', None)
        for value in (getattr(mobile, 'devices', None) or {}).values():
            if isinstance(value, dict) and not value.get('revoked'):
                paired.append(dict(name=value.get('name') or '手机',
                                   paired_at=_stamp(value.get('created_at'))))
    except Exception:
        paired = []
    online = []
    for item in phone.get('devices') or []:
        if not isinstance(item, dict):
            continue
        online.append(dict(name=item.get('name') or item.get('device_id') or '手机',
                           last_seen=_stamp(item.get('last_seen')),
                           has_stream=bool(item.get('has_stream'))))
    now = _stamp(time.time())
    return dict(
        desktop=dict(connected=bool(desktop.get('connected')),
                     seen_at=_stamp(desktop.get('updated_at') or desktop.get('at'))),
        phone=dict(connected=bool(phone.get('connected')),
                   # devices 保留旧字段，它就是「现在真在线」的那批，避免下游读空。
                   devices=[row['name'] for row in online][:3],
                   paired=paired[:5],
                   online=online[:5],
                   observed_at=now,
                   basis='online=最近一次注册在 90 秒窗口内；paired 只是配对过的存档，'
                         '不代表现在连得上'),
    )


def _clock(core, now):
    return dict(
        now=_stamp(now),
        now_ts=now,
        timezone='Asia/Shanghai',
        processed_started_at=_stamp(getattr(core, 'process_started_at', None)),
        last_sleep_at=_stamp(core._state('last_sleep_at')),
        last_wake_at=_stamp(core._state('last_wake_at')),
    )


def _duplicate_hints(core):
    """找出目标高度重叠的多个在用项目，交给她自己判断要不要合并。"""
    rows=[]
    for project in core._projects():
        if project.get('status') not in {'active','waiting','paused'}:
            continue
        goal=str(project.get('goal') or '').strip()
        if goal:
            rows.append((project.get('key') or project.get('ref'), goal))
    hints=[]
    for index,(key_a,goal_a) in enumerate(rows):
        tokens_a=set(goal_a)
        for key_b,goal_b in rows[index+1:]:
            union=tokens_a|set(goal_b)
            if not union:
                continue
            overlap=len(tokens_a & set(goal_b))/len(union)
            if overlap>=0.5:
                hints.append(dict(keys=[key_a,key_b],overlap=round(overlap,2),
                                  note='同一件事可能记了两个项目；要不要合并、留哪个由你决定'))
    return hints[:3]


def build(core, *, now=None, attention=None, current=None):
    """返回只读投影。字段为 None / 空表示未知，调用方不能自行补造。"""
    now = time.time() if now is None else float(now)
    errors = []
    state = dict(
        generated_at=_stamp(now),
        generated_at_ts=now,
        projection_version=PROJECTION_VERSION,
        self_ref=dict(persona_hash=core._state('persona_hash')),
        body=_section('body', errors, lambda: _body(core, now)),
        affect=_section('affect', errors, lambda: _affect(core, now)),
        mood=_section('mood', errors, lambda: _mood(core, now)),
        motivation=_section('motivation', errors, lambda: _motivation(core, attention)),
        current_activity=_section('current_activity', errors, lambda: _current_activity(core, now), []),
        commitments=_section('commitments', errors, lambda: _commitments(core, now), []),
        pending_expression=_pending_expression(core, now),
        attention=_section('attention', errors, lambda: _attention(core, now)),
        presence=_section('presence', errors, lambda: _presence(core)),
        clock=_section('clock', errors, lambda: _clock(core, now)),
    )
    # 可能重复的项目只做提示，不自动合并：名称相似不等于同一件事。
    state['possible_duplicates'] = _duplicate_hints(core)

    # 同一事实只出现一次：已经作为"约定"列出的项目不再重复出现在当前活动里。
    promised = {row.get('ref') for row in (state.get('commitments') or [])}
    state['current_activity'] = [row for row in (state.get('current_activity') or [])
                                 if row.get('ref') not in promised]
    if current is not None:
        state['with_whom'] = dict(conversation_id=getattr(core.scope, 'conversation_id', None),
                                  source_kind=getattr(current, 'source_kind', None))
    if errors:
        # 只在运行详情里可见，用于定位，不进普通聊天。
        state['projection_errors'] = errors
    return state
