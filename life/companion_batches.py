# -*- coding: utf-8 -*-
r"""事件已发生 ≠ 她已收到：六级投递、批次溯源与续接。

为什么要有这一层
----------------
以前「发生了什么」（事件）和「她什么时候知道的」（投递）混在同一份上下文里：
后台明细和刚刚影响到当前动作的事挤在一起，模型这一轮看到什么全靠拼装顺序，
于是出现两种失真——**该立刻知道的被淹掉**，**已经说过的被再说一遍**。

这一层把两件事拆开：

- **事件**：授权范围内可观察的事实，各自带 ``event_id``、来源类型、来源引用
  （``source_ref`` / ``action_ref``）、发生时间、受众范围与遗忘纪元。事件只被记录，
  不因为「级别低」被丢掉，也不被拼成一句话。
- **批次**：同一级别、同一话题的事件在一个时间窗口里合成一批，带 ``coverage``
  （覆盖了哪段时间/哪些事件）与 ``unexpanded``（还有多少没展开、为什么）。
  批次是**投递单位**，不是内容单位：每条事件在批次里仍是独立条目。
- **台账**：批次真正装进模型请求且请求成功之后，才写 ``delivered``。模型失败、
  进程重启都不写——同一批事件下一轮仍在 ``pending`` 里，能续接。

六级只决定「什么时候送」，不决定「什么值得她知道」：级别不会删除、截断或降权
事件内容，只会改变它出现在哪一批、哪一轮。级别越高（数字越大）越靠后：
后台计数不该把「当前动作已被停止」挤掉。

六个级别
--------
1 ``IMMEDIATE``   立即处理：影响当前动作。必要的停止由执行器先落实，这里只记
                  ``stop_state``（已停/未确认），不替执行器假装已停。
2 ``CONVERSATION``当前对话：短窗口合批，保留每条原话与顺序（编号列表，不是拼接）。
3 ``AWAITED``     正在等的结果：同一任务的相关变化攒一批，结果到了就能续接。
4 ``STATE``       状态变化：下一次自然认知时送；若正影响当前任务则升级为 3，
                  并注明何时、由谁、不知道就写不知道。
5 ``DAILY``       日常经历：按场景或时间合批，不逐条叫醒她。
6 ``BACKGROUND``  后台明细：只给计数/时间范围/可查引用；异常或订阅命中可提前。

订阅（``companion_subscriptions``）
----------------------------------
她可以自己说「这个子任务回来就告诉我」（``task:<task_ref>`` / ``agent:*``），
也可以说「索引进度不用叫我」（``index_progress`` + ``notify=0``）。
``notify=1`` 的订阅命中可以把 5/6 级提前到 3 级；``notify=0`` 明确压回 6 级。
订阅只改投递时机，不改内容。

采集边界
--------
事件的 ``scope_id`` 必须等于当前会话（不采别的会话正文）；事件的 ``epoch`` 是
创建时的 ``repo.forget_epoch(scope_id)``——读取时若当前纪元已经前进（材料被遗忘），
旧批次不会再出现。这样遗忘过的东西不能靠一份旧批次复活。
"""
from __future__ import annotations

import json
import logging
import time

from .text import stable_id

log = logging.getLogger(__name__)

# ------------------------------------------------------------------ 级别
IMMEDIATE = 1
CONVERSATION = 2
AWAITED = 3
STATE = 4
DAILY = 5
BACKGROUND = 6

LEVELS = (IMMEDIATE, CONVERSATION, AWAITED, STATE, DAILY, BACKGROUND)
LEVEL_NAMES = {
    IMMEDIATE: '立即处理',
    CONVERSATION: '当前对话',
    AWAITED: '正在等的结果',
    STATE: '状态变化',
    DAILY: '日常经历',
    BACKGROUND: '后台明细',
}
# 级别只决定什么时候送：每个级别对应的「送法」写在这里，供视图与回执共用。
LEVEL_TIMING = {
    IMMEDIATE: '这一轮就送；必要的停止由执行器先落实',
    CONVERSATION: '短窗口合批，多条原话按顺序各占一条',
    AWAITED: '同一任务的变化攒一批，结果到了就能续接',
    STATE: '下一次自然认知时送；影响当前任务时升级为「正在等的结果」',
    DAILY: '按场景或时间合批，不逐条叫醒',
    BACKGROUND: '只给计数/时间范围/可查引用；异常或订阅命中可提前',
}

# ------------------------------------------------------------------ 阈值
# 阈值可配置：ResourcePolicy 的字段名见 POLICY_FIELDS，也可以在 cfg.resource_policy
# 的 dict 里直接写同名键。默认值按「不吵醒她、但也不会让事件永远积压」取。
DEFAULT_THRESHOLDS = {
    'conversation_window_seconds': 20,
    'state_window_seconds': 900,
    'daily_window_seconds': 1800,
    'background_window_seconds': 3600,
    'max_events_per_batch': 12,
    'max_batches_per_turn': 4,
}
# 「正在等的结果」的覆盖窗口只用来说明这批覆盖了哪段时间，**不参与何时送**：
# 结果到了就该送，等窗口反而是在压她的消息。所以它是常量，不是可配置的旋钮。
AWAITED_WINDOW_SECONDS = 600
POLICY_FIELDS = {
    'conversation_window_seconds': 'delivery_conversation_window_seconds',
    'state_window_seconds': 'delivery_state_window_seconds',
    'daily_window_seconds': 'delivery_daily_window_seconds',
    'background_window_seconds': 'delivery_background_window_seconds',
    'max_events_per_batch': 'delivery_max_events_per_batch',
    'max_batches_per_turn': 'delivery_max_batches_per_turn',
}


def thresholds(core=None):
    """合并默认阈值与部署配置；读不到就用默认值，绝不因为配置坏了而丢事件。"""
    out = dict(DEFAULT_THRESHOLDS)
    raw = None
    cfg = getattr(core, 'cfg', None)
    if cfg is not None:
        raw = getattr(cfg, 'resource_policy', None)
    if raw is None:
        raw = getattr(core, 'resources', None)
    if raw is None:
        return out
    for key, field in POLICY_FIELDS.items():
        value = None
        if isinstance(raw, dict):
            value = raw.get(key, raw.get(field))
        else:
            value = getattr(raw, field, None)
        if type(value) is int and value > 0:
            out[key] = value
        elif isinstance(value, float) and value > 0:
            out[key] = int(value)
    return out


# ------------------------------------------------------------------ 存储
SCHEMA = """
CREATE TABLE IF NOT EXISTS companion_batch_events (
  event_id     TEXT PRIMARY KEY,
  agent_id     TEXT NOT NULL,
  scope_id     TEXT NOT NULL DEFAULT '',
  epoch        INTEGER NOT NULL DEFAULT 0,
  source_type  TEXT NOT NULL,
  source_ref   TEXT NOT NULL DEFAULT '',
  action_ref   TEXT NOT NULL DEFAULT '',
  topic        TEXT NOT NULL DEFAULT '',
  level        INTEGER NOT NULL DEFAULT 4,
  title        TEXT NOT NULL DEFAULT '',
  detail       TEXT NOT NULL DEFAULT '',
  audience     TEXT NOT NULL DEFAULT '',
  affects_current_action INTEGER NOT NULL DEFAULT 0,
  stop_state   TEXT NOT NULL DEFAULT '',
  payload      TEXT NOT NULL DEFAULT '{}',
  occurred_at  REAL NOT NULL,
  created_at   REAL NOT NULL,
  batch_id     TEXT NOT NULL DEFAULT '',
  state        TEXT NOT NULL DEFAULT 'pending',
  delivered_at REAL
);
CREATE INDEX IF NOT EXISTS ix_companion_batch_events_pending
  ON companion_batch_events(agent_id, scope_id, state, occurred_at);

CREATE TABLE IF NOT EXISTS companion_batches (
  batch_id     TEXT PRIMARY KEY,
  agent_id     TEXT NOT NULL,
  scope_id     TEXT NOT NULL DEFAULT '',
  epoch        INTEGER NOT NULL DEFAULT 0,
  level        INTEGER NOT NULL,
  topic        TEXT NOT NULL DEFAULT '',
  wake_reason  TEXT NOT NULL DEFAULT '',
  coverage     TEXT NOT NULL DEFAULT '{}',
  unexpanded   TEXT NOT NULL DEFAULT '{}',
  context      TEXT NOT NULL DEFAULT '{}',
  state        TEXT NOT NULL DEFAULT 'pending',
  attempts     INTEGER NOT NULL DEFAULT 0,
  last_error   TEXT NOT NULL DEFAULT '',
  created_at   REAL NOT NULL,
  updated_at   REAL NOT NULL,
  delivered_at REAL
);

CREATE TABLE IF NOT EXISTS companion_batch_deliveries (
  batch_id    TEXT NOT NULL,
  decision_id TEXT NOT NULL,
  agent_id    TEXT NOT NULL,
  scope_id    TEXT NOT NULL DEFAULT '',
  level       INTEGER NOT NULL,
  event_ids   TEXT NOT NULL DEFAULT '[]',
  coverage    TEXT NOT NULL DEFAULT '{}',
  unexpanded  TEXT NOT NULL DEFAULT '{}',
  model_ok    INTEGER NOT NULL DEFAULT 0,
  reason      TEXT NOT NULL DEFAULT '',
  at          REAL NOT NULL,
  PRIMARY KEY (batch_id, decision_id)
);

-- 逐事件送达行：一事件一行，和批次行、事件行三处能逐条对账。
CREATE TABLE IF NOT EXISTS companion_batch_delivery_events (
  batch_id    TEXT NOT NULL,
  decision_id TEXT NOT NULL,
  event_id    TEXT NOT NULL,
  agent_id    TEXT NOT NULL,
  scope_id    TEXT NOT NULL DEFAULT '',
  level       INTEGER NOT NULL,
  at          REAL NOT NULL,
  PRIMARY KEY (batch_id, decision_id, event_id)
);

CREATE TABLE IF NOT EXISTS companion_subscriptions (
  subscription_id TEXT PRIMARY KEY,
  agent_id        TEXT NOT NULL,
  scope_id        TEXT NOT NULL DEFAULT '',
  topic           TEXT NOT NULL,
  notify          INTEGER NOT NULL DEFAULT 1,
  note            TEXT NOT NULL DEFAULT '',
  created_at      REAL NOT NULL,
  updated_at      REAL NOT NULL
);
"""


def ensure(repo):
    with repo.tx():
        for statement in SCHEMA.strip().split(';'):
            if statement.strip():
                repo._exec(statement)


# ------------------------------------------------------------------ 读 core
def _repo(core):
    repo = getattr(core, 'repo', None)
    if repo is None:
        raise ValueError('batch_events_require_repo')
    return repo


def _agent_id(core):
    life = getattr(core, 'life', None)
    value = getattr(life, 'agent_id', None) or getattr(core, 'agent_id', None)
    return str(value or 'default')


def _scope_id(core):
    scope = getattr(core, 'scope', None)
    value = getattr(scope, 'scope_id', None)
    return str(value or '')


def _epoch(core):
    repo, scope_id = _repo(core), _scope_id(core)
    try:
        return int(repo.forget_epoch(scope_id))
    except Exception:  # noqa: BLE001 - 读不到纪元就不采（宁可少记，不可漏遗忘）
        log.warning('遗忘纪元读取失败，本批事件按 0 处理', exc_info=True)
        return 0


def _now(now=None):
    return time.time() if now is None else float(now)


# ------------------------------------------------------------------ 事件
def make_event(*, event_id, source_type, title, detail='', source_ref='', action_ref='',
               occurred_at=None, topic='', audience='', affects_current_action=False,
               stop_state='', payload=None, now=None):
    """一个事件 = 一件独立的事。字段固定，方便回执、台账与按需读取对上号。"""
    at = _now(now)
    return dict(event_id=str(event_id), source_type=str(source_type), source_ref=str(source_ref or ''),
                action_ref=str(action_ref or ''), topic=str(topic or ''), title=str(title or ''),
                detail=str(detail or ''), occurred_at=float(occurred_at if occurred_at is not None else at),
                audience=str(audience or ''), affects_current_action=bool(affects_current_action),
                stop_state=str(stop_state or ''), payload=dict(payload or {}))


def event_id_for(source_type, *parts):
    return stable_id('batch-event-' + str(source_type), *[str(p) for p in parts])


# ------------------------------------------------------------------ 分类
# source_type -> 默认级别。异常/订阅命中/影响当前动作会在这里的基础上调整。
SOURCE_LEVELS = {
    'interjection': IMMEDIATE,
    'current_action_stopped': IMMEDIATE,
    'user_input': CONVERSATION,
    'awaited_result': AWAITED,
    'delivery_unknown': AWAITED,
    'delivery_failed': AWAITED,
    'delivery_pending': AWAITED,
    'index_progress': BACKGROUND,
    'index_anomaly': AWAITED,
    'agent_result': AWAITED,
    'agent_progress': BACKGROUND,
    'state_change': STATE,
    'daily_experience': DAILY,
    'background_detail': BACKGROUND,
}


def _base_level(event):
    source_type = str(event.get('source_type') or '')
    if source_type in SOURCE_LEVELS:
        return SOURCE_LEVELS[source_type]
    if event.get('affects_current_action'):
        return IMMEDIATE
    return STATE


def classify(event, *, thresholds_=None, subscriptions=(), affects_current_action=None):
    """事件该按几级送。只影响时机，不决定内容去留。

    优先级是硬的：**影响当前动作 → 1（立即）先判**，任何订阅都不能把它压后。
    她喊停过的事、当前动作被停止，哪怕她订阅了「这类不用叫我」，也必须立刻送到。
    其余才轮到订阅：notify=1 命中把 5/6 提前到 3；notify=0 压回 6。
    """
    del thresholds_  # 阈值目前只影响窗口，不影响级别；保留参数给未来按负载调整
    if affects_current_action is None:
        affects_current_action = bool(event.get('affects_current_action'))
    if affects_current_action:
        return IMMEDIATE
    level = _base_level(event)
    hit = match_subscription(subscriptions, event)
    if hit is not None:
        if not hit.get('notify', True):
            return BACKGROUND
        if level in (DAILY, BACKGROUND):
            level = AWAITED
    if level == STATE and event.get('payload', {}).get('affects_current_task'):
        return AWAITED
    return level


def topic_for(event, level):
    """同批的话题键：同任务、同场景、同会话才合批，别把不相干的事并成一句。"""
    explicit = str(event.get('topic') or '').strip()
    if explicit:
        return explicit
    source_type = str(event.get('source_type') or '')
    payload = event.get('payload') or {}
    if level == IMMEDIATE:
        return 'immediate:' + (str(event.get('source_ref') or '') or source_type)
    if level == CONVERSATION:
        return 'conversation'
    if level == AWAITED:
        ref = payload.get('task_ref') or payload.get('agent_id') or payload.get('project_ref') or event.get('source_ref')
        return 'await:' + (str(ref or '') or source_type)
    if level == STATE:
        return 'state:' + source_type
    if level == DAILY:
        return 'daily:' + str(payload.get('scene') or source_type)
    return 'background:' + source_type


# ------------------------------------------------------------------ 订阅
def _row_subscription(row):
    return dict(subscription_id=row['subscription_id'], topic=row['topic'],
                notify=bool(row['notify']), note=row['note'],
                scope_id=row['scope_id'], created_at=row['created_at'],
                updated_at=row['updated_at'])


def subscriptions(core):
    ensure(_repo(core))
    rows = _repo(core)._all('SELECT * FROM companion_subscriptions WHERE agent_id=? AND scope_id IN (?,?)',
                            (_agent_id(core), _scope_id(core), ''))
    return [_row_subscription(row) for row in rows]


def subscribe(core, topic, *, notify=True, note='', now=None):
    """她说的「这个回来就告诉我 / 这个不用叫我」落成一条订阅。

    订阅只影响什么时候送；``notify=False`` 的订阅不会丢弃事件，只是把它压回后台
    明细，等下一次自然合批时按计数给。
    """
    topic = str(topic or '').strip()
    if not topic:
        raise ValueError('subscription_requires_topic')
    repo, at = _repo(core), _now(now)
    ensure(repo)
    subscription_id = stable_id('batch-sub', _agent_id(core), _scope_id(core), topic)
    with repo.tx():
        repo._exec('INSERT OR REPLACE INTO companion_subscriptions'
                   '(subscription_id,agent_id,scope_id,topic,notify,note,created_at,updated_at)'
                   ' VALUES(?,?,?,?,?,?,?,?)',
                   (subscription_id, _agent_id(core), _scope_id(core), topic, 1 if notify else 0,
                    str(note or ''), at, at))
    return dict(subscription_id=subscription_id, topic=topic, notify=bool(notify),
                note=str(note or ''), at=at)


def unsubscribe(core, topic):
    repo = _repo(core)
    ensure(repo)
    with repo.tx():
        repo._exec('DELETE FROM companion_subscriptions WHERE agent_id=? AND topic=?',
                   (_agent_id(core), str(topic or '')))
    return True


def _topic_matches(pattern, topic, event):
    pattern = str(pattern or '').strip()
    if not pattern:
        return False
    if pattern in ('*', 'all'):
        return True
    if pattern == topic:
        return True
    if pattern.endswith('*') and topic.startswith(pattern[:-1]):
        return True
    # 'task:xxx' 也应命中该任务的事件（事件话题可能是 await:xxx）
    if pattern.startswith('task:'):
        ref = pattern.split(':', 1)[1]
        payload = event.get('payload') or {}
        return ref in {str(payload.get(key) or '') for key in ('task_ref', 'agent_id')} or \
            str(event.get('source_ref') or '') == ref
    return False


def _topic_candidates(event):
    """一个事件可能被哪些订阅话题命中：话题键、来源类型、任务引用都算。"""
    level = _base_level(event)
    payload = event.get('payload') or {}
    topics = {topic_for(event, level), str(event.get('topic') or ''),
              str(event.get('source_type') or '')}
    for key in ('task_ref', 'agent_id', 'project_ref'):
        value = str(payload.get(key) or '')
        if value:
            topics.add('task:' + value)
    return {topic for topic in topics if topic}


def match_subscription(subs, event):
    candidates = _topic_candidates(event)
    for item in subs or ():
        pattern = item.get('topic')
        if any(_topic_matches(pattern, topic, event) for topic in candidates):
            return item
    return None


# ------------------------------------------------------------------ 采集
def _safe(core, label, fn, default):
    try:
        return fn()
    except Exception:  # noqa: BLE001 - 采集失败只少一类事实，不能弄坏整轮
        log.warning('批次事件采集失败：%s', label, exc_info=True)
        return default


def _conversation_ref(core):
    return str(getattr(getattr(core, 'scope', None), 'conversation_id', '') or '')


def _owner_private(core):
    """只在主人私聊里收「她自己的日常经历」：群聊里不把私聊内容带过去。"""
    scope = getattr(core, 'scope', None)
    owner = str(getattr(getattr(core, 'cfg', None), 'owner', '') or '')
    participant = str(getattr(scope, 'participant_id', '') or '')
    conversation = str(getattr(scope, 'conversation_id', '') or '')
    return bool(owner and participant == owner and ':private:' in conversation)


DELIVERY_SOURCE_TYPES = ('delivery_pending', 'delivery_unknown', 'delivery_failed')
# 子任务台账里「没有会话」的行是 **agent 级**，不是「空字符串 scope」。
# 以前查询用 scope_id='' 兜底，等于让空串冒充 agent 级；现在显式写 agent。
AGENT_SCOPE = 'agent'


def _readable(stamp):
    """时间给人看：带秒与时区偏移。缺值返回 None，不编。"""
    if not stamp:
        return None
    try:
        return time.strftime('%Y-%m-%d %H:%M:%S %z', time.localtime(float(stamp)))
    except (TypeError, ValueError, OSError):
        return None


def _delivery_timeline(repo, agent_id, message_id):
    """同一条消息在事件台账里的状态时间线（按发生顺序）。"""
    try:
        rows = repo._all(
            "SELECT source_type,payload,occurred_at FROM companion_batch_events "
            "WHERE agent_id=? AND source_type IN ('delivery_pending','delivery_unknown','delivery_failed') "
            "AND json_extract(payload,'$.message_id')=? ORDER BY created_at ASC",
            (agent_id, str(message_id)))
    except Exception:  # noqa: BLE001 - 台账还没建/查询失败：时间线未知，但不影响这条事实
        log.warning('投递事件时间线读取失败', exc_info=True)
        return []
    timeline = []
    for row in rows:
        try:
            payload = json.loads(row['payload'] or '{}')
        except ValueError:
            payload = {}
        state = str(payload.get('state') or row['source_type'][len('delivery_'):])
        if timeline and timeline[-1]['state'] == state:
            continue
        timeline.append(dict(state=state, at=_readable(row['occurred_at'])))
    return timeline


def events_from_delivery(core, *, now=None, limit=6):
    """她自己说过的话：可能已发出、未确认；明确失败；还在发。**不重发**。

    这里只读投递台账（``companion_delivery``）。状态 ``unknown`` 保持 unknown：
    不改成失败、也不触发任何重发——那是她要知道的事实，不是要系统替她做的决定。
    只收本会话（以及没有指定会话）的投递，别的会话的正文不进这一批。

    同一条消息的状态会变（发送中 → 失败/未确认），所以事件 id 带上状态，并记录
    「之前是 X，现在是 Y」的时间线；旧的那条 pending 事实会被作废（见 ``record``），
    台账不会永远停在「还在发送中」。
    """
    from . import companion_delivery as ledger
    at = _now(now)
    repo, agent_id = _repo(core), _agent_id(core)
    ensure(repo)
    conversation = _conversation_ref(core)
    rows = ledger.pending(repo, agent_id=agent_id, now=at, limit=int(limit) * 3)
    out = []
    for row in rows:
        route = str(row.get('conversation_ref') or '')
        if route and conversation and route != conversation:
            continue
        if len(out) >= int(limit):
            break
        state = str(row.get('state') or '')
        if state == 'unknown':
            source_type, title = 'delivery_unknown', '这条可能已经发出、但没有收到确认'
            level_hint = AWAITED
        elif state == 'failed':
            source_type, title = 'delivery_failed', '这条明确没有送出去'
            level_hint = AWAITED
        else:
            source_type, title = 'delivery_pending', '这条还在发送中'
            level_hint = AWAITED
        detail = str(row.get('reason') or '')
        if row.get('sent_at'):
            detail = (detail + '；' if detail else '') + '交给传输的时间 ' + time.strftime(
                '%Y-%m-%d %H:%M:%S', time.localtime(float(row['sent_at'])))
        if state in RESOLVED_STATES:
            # 已经交出去的消息不该再报「不知道到没到」（供 select 时已解析的路径使用）。
            title = '这条已经发出去了'
            detail = _resolved_detail(row)
        elif state == 'failed':
            detail = _failed_detail(row)
        else:
            # 只陈述事实：不写「由你决定要不要再说一次」——那会诱发她把刚说过的话再说一遍。
            detail = _pending_detail(dict(row, reason=detail))
        message_id = row.get('message_id')
        timeline = _delivery_timeline(repo, agent_id, message_id)
        previous = timeline[-1] if timeline else None
        if previous and previous.get('state') != state:
            detail += '；之前是%s（%s），现在是%s（%s）' % (
                _STATE_TEXT.get(str(previous.get('state')), previous.get('state')),
                previous.get('at') or '时间未记录',
                _STATE_TEXT.get(state, state), _readable(at))
        if not timeline or timeline[-1]['state'] != state:
            timeline = timeline + [dict(state=state, at=_readable(at))]
        out.append(make_event(
            event_id=event_id_for('delivery', message_id, state),
            source_type=source_type, title=title, detail=detail,
            source_ref=str(message_id or ''), action_ref=str(row.get('event_ref') or ''),
            occurred_at=row.get('queued_at') or row.get('updated_at') or at,
            topic='delivery:' + str(row.get('turn_id') or ''), audience=str(row.get('conversation_ref') or ''),
            payload=dict(message_id=message_id, state=state,
                         previous_state=(previous or {}).get('state'),
                         timeline=timeline,
                         turn_id=row.get('turn_id'), segment=row.get('segment'),
                         channels=json.loads(row.get('channels') or '[]'),
                         sent_at=row.get('sent_at'), level_hint=level_hint),
            now=at))
    return out


_STATE_TEXT = {'queued': '排队', 'sending': '发送中', 'sent': '已生成交给传输',
               'rendered': '端上已呈现', 'failed': '明确失败', 'unknown': '结果未知'}

# 已交出去的状态：这些状态下「这条可能没送到」已经不成立了，文案要改成已解决的事实。
RESOLVED_STATES = ('sent', 'rendered')


def _clock(stamp):
    """只给时分秒（她说「01:25:07」就是这种读法）。"""
    if not stamp:
        return None
    try:
        return time.strftime('%H:%M:%S', time.localtime(float(stamp)))
    except (TypeError, ValueError, OSError):
        return None


def _delivery_row(repo, agent_id, message_id):
    """按 message_id 重读投递台账的**当前**行；读不到返回 None。"""
    if not message_id:
        return None
    row = repo._one('SELECT message_id,turn_id,segment,conversation_ref,channels,state,reason,event_ref,'
                    'queued_at,updated_at,sent_at,rendered_at FROM companion_deliveries '
                    'WHERE agent_id=? AND message_id=?', (agent_id, str(message_id)))
    return dict(row) if row is not None else None


def _resolved_detail(row):
    """已发出的写法：只说事实 + 真实发送时间，不写「不知道到没到」。"""
    stamp = row.get('sent_at') or row.get('rendered_at') or row.get('updated_at')
    clock = _clock(stamp)
    detail = '这条已经发出去了（%s）' % (clock or '时间未记录')
    if row.get('rendered_at'):
        detail += '；端上回执 %s' % _clock(row['rendered_at'])
    return detail


def _pending_detail(row):
    """仍无回执的写法：只陈述事实，不写「由你决定要不要再说一次」这种会诱发重复的引导。"""
    detail = str(row.get('reason') or '').strip()
    base = '不知道它到没到；如果对话里已经出现就不用再说一次'
    return (detail + '；' if detail else '') + base


def _failed_detail(row):
    """明确失败的写法：原因 + 时间 + 不用等回执；不写「不知道它到没到」。"""
    reason = str(row.get('reason') or '').strip()
    detail = '这条明确没有送出去（%s）' % (_clock(row.get('updated_at')) or '时间未记录')
    if reason:
        detail += '；原因：' + reason[:120]
    return detail + '；不用等它的回执'


def refresh_delivery_events(core, *, now=None, message_ids=None):
    """送达前重读：``delivery_pending`` / ``delivery_unknown`` 事件按台账**当前**状态重写。

    事故背景（2026-11-21 01:25）：事件在 01:25:00 装批时抄了 state=queued 的快照，
    01:25:07 那段其实已经发出去了，01:25:13 她才看到旧快照，于是把刚说过的话又说了一遍。

    三种当前状态各自成句，互不拼接（t30 评审 L1–L3）：
    - 已 ``sent``/``rendered`` →「这条已经发出去了（时间）」，时间缺失时回退到 rendered_at；
    - ``failed`` →「这条明确没有送出去（时间）；原因：…；不用等它的回执」；
    - 仍 queued/sending/unknown → 只陈述事实。
    ``delivery_unknown`` 也必须重读：迟到确认后它还挂着「可能已经发出、但没有确认」就是过期情报。
    不改投递状态机，也不重发。
    """
    repo, agent_id = _repo(core), _agent_id(core)
    ensure(repo)
    at = _now(now)
    sql = ("SELECT event_id,source_type,payload,title,detail FROM companion_batch_events "
           "WHERE agent_id=? AND state='pending' "
           "AND source_type IN ('delivery_pending','delivery_unknown')")
    params = [agent_id]
    if message_ids:
        ids = [str(item) for item in message_ids if item]
        if not ids:
            return []
        sql += " AND json_extract(payload,'$.message_id') IN (%s)" % ','.join('?' * len(ids))
        params.extend(ids)
    try:
        rows = repo._all(sql, tuple(params))
    except Exception:  # noqa: BLE001 - 刷新失败不能拖垮这一轮：宁可旧事实，也不要错误事实
        log.warning('投递事实刷新读取失败', exc_info=True)
        return []
    updated = []
    with repo.tx():
        for row in rows:
            try:
                payload = json.loads(row['payload'] or '{}')
            except ValueError:
                payload = {}
            mid = str(payload.get('message_id') or '')
            current = _delivery_row(repo, agent_id, mid)
            if not current:
                continue
            state = str(current.get('state') or '')
            timeline = [dict(item) for item in (payload.get('timeline') or []) if isinstance(item, dict)]
            seen = {item.get('state') for item in timeline}
            if state in RESOLVED_STATES:
                sent_at = current.get('sent_at')
                rendered_at = current.get('rendered_at')
                # L3：UNKNOWN→RENDERED 没有经过 mark_sent 时 sent_at 为空，时间回退到 rendered_at。
                stamp = sent_at or rendered_at or current.get('updated_at') or at
                basis = ('sent_at' if sent_at else
                         'rendered_at' if rendered_at else 'updated_at')
                title = '这条已经发出去了'
                detail = _resolved_detail(current)
                payload['state'] = 'resolved'
                payload['resolved_state'] = state
                payload['sent_at'] = sent_at
                payload['resolved_at'] = stamp
                payload['time_basis'] = basis
                payload.pop('failure_reason', None)
                if 'queued' not in seen:
                    first_at = (payload.get('timeline') or [{}])[0].get('at') if payload.get('timeline') else None
                    timeline.insert(0, dict(state='queued', at=first_at))
                if 'sent' not in seen:
                    timeline.append(dict(state='sent', at=_readable(stamp), basis=basis))
                if state == 'rendered' and 'rendered' not in seen:
                    timeline.append(dict(state='rendered',
                                         at=_readable(rendered_at or stamp)))
            elif state == 'failed':
                # L2：明确失败要有自己的句子，不能和「还没有回执／不知道到没到」拼在一起。
                title = '这条明确没有送出去'
                detail = _failed_detail(current)
                payload['state'] = 'failed'
                payload['failed_at'] = current.get('updated_at')
                payload['failure_reason'] = str(current.get('reason') or '')
                payload.pop('time_basis', None)
                if 'failed' not in seen:
                    timeline.append(dict(state='failed',
                                         at=_readable(current.get('updated_at') or at)))
            else:
                title = '这条还没有回执'
                detail = _pending_detail(current)
                payload['state'] = state or 'pending'
                payload.pop('time_basis', None)
            timeline.sort(key=lambda item: (item.get('at') or ''))
            payload['timeline'] = timeline
            repo._exec('UPDATE companion_batch_events SET title=?,detail=?,payload=? WHERE event_id=?',
                       (title, detail, json.dumps(payload, ensure_ascii=False), row['event_id']))
            updated.append(dict(event_id=row['event_id'], title=title, detail=detail,
                                payload=payload, state=payload.get('state')))
    return updated


def refresh_views(core, batches, *, now=None):
    """把已经取出的批次视图按当前状态改写（请求体在装批与送达之间可能过期）。"""
    message_ids = []
    for batch in batches or ():
        for event in batch.get('events') or ():
            if event.get('source_type') in ('delivery_pending', 'delivery_unknown'):
                mid = event.get('source_ref')
                if mid:
                    message_ids.append(mid)
    if not message_ids:
        return batches
    fresh = {item['event_id']: item for item in refresh_delivery_events(
        core, now=now, message_ids=message_ids)}
    if not fresh:
        return batches
    for batch in batches or ():
        for event in batch.get('events') or ():
            item = fresh.get(event.get('event_id'))
            if not item:
                continue
            event['title'] = item['title']
            event['detail'] = item['detail']
            event['state'] = item['state']
    return batches


def events_from_agent_tasks(core, *, now=None, limit=6):
    """子 agent：结果到了算「正在等的结果」，跑着算「后台明细」。

    没有会话的行是 agent 级（``AGENT_SCOPE``）：查询同时接受历史空串，
    但事件里显式写 ``task_scope='agent'``，不再让空串冒充 agent 级。
    """
    at = _now(now)
    repo, agent_id, scope_id = _repo(core), _agent_id(core), _scope_id(core)
    rows = repo._all('SELECT id,task_ref,status,verification,goal,attempt,updated,scope_id '
                     'FROM companion_agent_tasks '
                     'WHERE agent=? AND (scope_id=? OR scope_id IN ("", ?)) '
                     'ORDER BY updated DESC LIMIT ?',
                     (agent_id, scope_id, AGENT_SCOPE, int(limit)))
    out = []
    for row in rows:
        status = str(row['status'] or '')
        done = status in {'completed', 'succeeded', 'failed', 'interrupted', 'cancelled'}
        source_type = 'agent_result' if done else 'agent_progress'
        title = '子任务有结果了：%s' % status if done else '子任务仍在进行：%s' % status
        detail = '目标 %s' % (str(row['goal'] or '')[:80],)
        if row['verification']:
            detail += '；核验：%s' % str(row['verification'])[:120]
        out.append(make_event(
            event_id=event_id_for('agent', row['id'], status, row['attempt'], row['verification']),
            source_type=source_type, title=title, detail=detail,
            source_ref=str(row['task_ref'] or ''), action_ref=str(row['id'] or ''),
            occurred_at=row['updated'] or at,
            topic='task:' + str(row['task_ref'] or row['id'] or ''),
            payload=dict(agent_id=row['id'], task_ref=row['task_ref'], status=status,
                         attempt=row['attempt'], verification=row['verification'],
                         task_scope=('agent' if str(row['scope_id'] or '') in ('', AGENT_SCOPE)
                                     else 'conversation'),
                         task_scope_id=str(row['scope_id'] or AGENT_SCOPE)),
            now=at))
    return out


def events_from_index(core, *, now=None):
    """索引进度：默认只作后台明细（她说「不用叫我」就维持在这里）。"""
    at = _now(now)
    index = getattr(getattr(core, 'life', None), 'memory_index', None)
    status = getattr(index, 'status', None)
    if not callable(status):
        return []
    values = status() or {}
    state = dict(lexical=values.get('lexical'), semantic=values.get('semantic'),
                 related=values.get('related'), model=values.get('model_key'),
                 chunker=values.get('chunker_version'))
    anomaly = str(values.get('semantic') or '') in {'failed', 'error', 'broken'}
    return [make_event(
        event_id=event_id_for('index', json.dumps(state, sort_keys=True, ensure_ascii=False)),
        source_type='index_anomaly' if anomaly else 'index_progress',
        title='索引后端异常' if anomaly else '索引进度',
        detail='词法 %s；语义 %s；相关 %s' % (state['lexical'], state['semantic'], state['related']),
        source_ref='life/memory_index.py', occurred_at=at, topic='index_progress',
        payload=dict(state=state, scene='index'), now=at)]


def events_from_today(core, *, now=None, limit=8):
    """日常经历：同一场景（kind）按时间合批，不逐条叫醒。

    只在主人私聊里收：``companion_activity`` 是 agent 级的（没有会话列），
    带进群聊就会把私聊里的事说给别的受众。
    """
    at = _now(now)
    if not _owner_private(core):
        return []
    material = getattr(core, '_today_material', None)
    if not callable(material):
        return []
    rows = material() or []
    out = []
    for row in rows[:int(limit)]:
        text = str(row.get('text') or '').strip()
        if not text:
            continue
        out.append(make_event(
            event_id=event_id_for('daily', row.get('id')), source_type='daily_experience',
            title='今天的一件经历：%s' % (row.get('kind') or ''),
            detail=text[:180], source_ref=str(row.get('id') or ''),
            action_ref=str((row.get('source_refs') or [''])[0]),
            occurred_at=_epochish(row.get('at')) or at,
            topic='daily:' + str(row.get('kind') or ''),
            payload=dict(scene=row.get('kind'), refs=row.get('source_refs') or [],
                         evidence_scope=row.get('evidence_scope')), now=at))
    return out


def _epochish(stamp):
    """``_iso_time`` 的影子：拿不到就返回 0（用当前时间兜底）。"""
    if not stamp:
        return 0.0
    try:
        return time.mktime(time.strptime(str(stamp)[:19], '%Y-%m-%d %H:%M:%S'))
    except ValueError:
        return 0.0


def events_from_conversation(core, batch=None, *, now=None):
    """当前对话：每条原话一个事件，保留顺序；绝不合并成一句。"""
    at = _now(now)
    rows = batch if batch is not None else getattr(core, '_batch', None)
    out = []
    for index, part in enumerate(rows or ()):
        event = part.get('event') if isinstance(part, dict) else None
        text = str((getattr(event, 'text', '') if event is not None else '') or '').strip()
        if not text:
            continue
        occurred = float(part.get('at') or getattr(event, 'occurred_at', 0)
                         or getattr(event, 'created', 0) or at)
        out.append(make_event(
            event_id=event_id_for('user_input', getattr(event, 'id', '') or (str(at) + ':' + str(index))),
            source_type='user_input', title='她连着说的第 %d 段' % (index + 1),
            detail=text[:200], source_ref=str(getattr(event, 'id', '') or ''),
            occurred_at=occurred, topic='conversation',
            payload=dict(index=index, source=getattr(event, 'source', None)), now=at))
    return out


def events_from_observation(core, observation, *, now=None):
    """当前动作被停止/打断：立即级；`stop_state` 如实写「已停 / 未确认」。"""
    at = _now(now)
    out = []
    observation = observation or {}
    status = str(observation.get('status') or '')
    if status in {'cancelled', 'interrupted', 'stopped'}:
        stopped = bool(observation.get('stopped') or observation.get('cancel_confirmed'))
        out.append(make_event(
            event_id=event_id_for('action_stop', observation.get('id') or at, status),
            source_type='current_action_stopped',
            title='当前动作已被停止' if stopped else '当前动作被要求停止，但停止未确认',
            detail=str(observation.get('reason') or observation.get('detail') or '')[:160],
            source_ref=str(observation.get('id') or ''), occurred_at=at,
            topic='immediate:' + str(observation.get('id') or ''),
            affects_current_action=True, stop_state='stopped' if stopped else 'unconfirmed',
            payload=dict(status=status), now=at))
    for row in getattr(core, '_interjections', None) or ():
        text = str(row.get('text') or '').strip()
        if not text:
            continue
        out.append(make_event(
            event_id=event_id_for('interjection', text, row.get('at')),
            source_type='interjection', title='她说这句话时打断了我',
            detail=text[:200], source_ref=str((row.get('source') or {}).get('id') or ''),
            occurred_at=float(row.get('at') or at), topic='immediate:interjection',
            affects_current_action=True,
            stop_state=str(row.get('stop_state') or 'pending_bubbles_cancelled'),
            payload=dict(source=row.get('source')), now=at))
    return out


def _world_event(row, before='', *, now=None, purpose=None):
    state = str(row.get('state') or '')
    detail = '之前 %s，现在 %s' % (before or '没记录过', state)
    if row.get('observed_at'):
        detail += '；观察到 %s' % row['observed_at']
    if row.get('detail'):
        detail += '；%s' % str(row['detail'])[:120]
    # 世界入口只报当前状态，不报「谁改的」——不知道就写不知道。
    detail += '；由谁改变：不知道'
    return make_event(
        event_id=event_id_for('state', row.get('fact_ref'), state),
        source_type='state_change', title='%s：%s' % (row.get('subject') or row.get('fact_ref'), state),
        detail=detail, source_ref=str(row.get('source_ref') or ''), occurred_at=_now(now),
        payload=dict(fact_ref=row.get('fact_ref'), state=state, before=before,
                     affects_current_task=bool(purpose == 'work' and state != 'ready')),
        now=now)


def events_from_world(core, *, purpose=None, now=None):
    """世界状态**变了**才记一条（下一次自然认知送）；谁改的不知道就写不知道。

    基线（上一次读到的状态）存在 meta 里：第一次读只建立基线、不刷一堆「一切正常」。
    读不到基线（精简 core）时只报非 ready 的状态，仍按 event_id 去重。
    """
    at = _now(now)
    facts = getattr(core, '_world_facts', None)
    if not callable(facts):
        return []
    rows = [row for row in (facts() or []) if isinstance(row, dict) and row.get('fact_ref')]
    getter, setter = getattr(core, '_state', None), getattr(core, '_save', None)
    if not (callable(getter) and callable(setter)):
        return [_world_event(row, now=at, purpose=purpose) for row in rows
                if str(row.get('state') or '') != 'ready']
    try:
        previous = dict(getter('world_state_seen') or {})
    except Exception:  # noqa: BLE001
        previous = {}
    current = {str(row['fact_ref']): str(row.get('state') or '') for row in rows}
    events = []
    if previous:
        for row in rows:
            ref, state = str(row['fact_ref']), str(row.get('state') or '')
            if previous.get(ref) not in (None, state):
                events.append(_world_event(row, previous.get(ref), now=at, purpose=purpose))
    try:
        with _repo(core).tx():
            setter('world_state_seen', current)
    except Exception:  # noqa: BLE001 - 基线写不上只是下次多报一次，不弄坏这一轮
        log.warning('世界状态基线写入失败', exc_info=True)
    return events


def collect(core, *, current=None, observation=None, batch=None, purpose=None, now=None):
    """把这一轮所有可观察事件收齐（各采集器互相隔离，失败只少一类）。"""
    at = _now(now)
    events = []
    events += _safe(core, 'delivery', lambda: events_from_delivery(core, now=at), [])
    events += _safe(core, 'agent_tasks', lambda: events_from_agent_tasks(core, now=at), [])
    events += _safe(core, 'index', lambda: events_from_index(core, now=at), [])
    events += _safe(core, 'world', lambda: events_from_world(core, purpose=purpose, now=at), [])
    events += _safe(core, 'today', lambda: events_from_today(core, now=at), [])
    if current is not None or batch:
        events += _safe(core, 'conversation', lambda: events_from_conversation(core, batch, now=at), [])
    events += _safe(core, 'observation', lambda: events_from_observation(core, observation, now=at), [])
    return events


# ------------------------------------------------------------------ 入库
def record(core, events, *, now=None):
    """事件落库：同一 ``event_id`` 只记一次；同一件事的最新状态不留旧壳。

    - ``event_id`` 相同 → 不重复记（同一回执不重复叫醒她）。
    - 同一条消息的投递状态变了（发送中 → 失败/未确认）：新事件入库，旧的 pending
      投递事件作废（``state='superseded'``），台账只显示最新状态，时间线在 payload 里。
    - 记录的 ``level`` 是当时真正判出来的级别（含订阅/影响范围）；判不出来就记 0，
      由 ``unexpanded`` 归到 unknown，不再一律冒充「状态变化」。
    """
    repo, agent_id, scope_id, epoch = _repo(core), _agent_id(core), _scope_id(core), _epoch(core)
    at = _now(now)
    ensure(repo)
    limits, subs = thresholds(core), subscriptions(core)
    stored = []
    with repo.tx():
        for event in events or ():
            if not event.get('event_id'):
                continue
            level = event.get('level')
            if level in (None, ''):
                try:
                    level = classify(event, thresholds_=limits, subscriptions=subs)
                except Exception:  # noqa: BLE001 - 级别判不出来也要记事实，标成未知
                    log.warning('事件级别判定失败，记为未知', exc_info=True)
                    level = 0
            if event.get('source_type') in DELIVERY_SOURCE_TYPES:
                mid = str((event.get('payload') or {}).get('message_id') or '')
                if mid:
                    # 同一条消息换了状态：旧的「还在发送中」不能再留在 pending 里说谎。
                    repo._exec(
                        "UPDATE companion_batch_events SET state='superseded',batch_id='' "
                        "WHERE agent_id=? AND state='pending' AND event_id<>? "
                        "AND source_type IN ('delivery_pending','delivery_unknown','delivery_failed') "
                        "AND json_extract(payload,'$.message_id')=?",
                        (agent_id, event['event_id'], mid))
            repo._exec(
                'INSERT OR IGNORE INTO companion_batch_events'
                '(event_id,agent_id,scope_id,epoch,source_type,source_ref,action_ref,topic,level,title,detail,'
                'audience,affects_current_action,stop_state,payload,occurred_at,created_at,state)'
                ' VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (event['event_id'], agent_id, scope_id, epoch, event.get('source_type') or '',
                 event.get('source_ref') or '', event.get('action_ref') or '', event.get('topic') or '',
                 int(level), event.get('title') or '', event.get('detail') or '',
                 event.get('audience') or '', 1 if event.get('affects_current_action') else 0,
                 event.get('stop_state') or '',
                 json.dumps(event.get('payload') or {}, ensure_ascii=False),
                 float(event.get('occurred_at') or at), at, 'pending'))
            stored.append(event['event_id'])
    return stored


def _row_event(row):
    return dict(event_id=row['event_id'], source_type=row['source_type'], source_ref=row['source_ref'],
                action_ref=row['action_ref'], topic=row['topic'], level=int(row['level']),
                title=row['title'], detail=row['detail'], audience=row['audience'],
                affects_current_action=bool(row['affects_current_action']), stop_state=row['stop_state'],
                payload=json.loads(row['payload'] or '{}'),
                occurred_at=float(row['occurred_at']), created_at=float(row['created_at']),
                state=row['state'], batch_id=row['batch_id'], epoch=int(row['epoch']),
                scope_id=row['scope_id'])


def _expire_forgotten(repo, agent_id, scope_id, epoch):
    """遗忘纪元前进之后：旧纪元没送出去的事件不再出现，也不留空壳批次冒充「未送达」。"""
    with repo.tx():
        repo._exec("UPDATE companion_batch_events SET state='forgotten',batch_id='' "
                   "WHERE agent_id=? AND scope_id=? AND state='pending' AND epoch<>?",
                   (agent_id, scope_id, epoch))
        repo._exec("DELETE FROM companion_batches WHERE agent_id=? AND scope_id=? AND state='pending' "
                   "AND epoch<>? AND batch_id NOT IN (SELECT DISTINCT batch_id FROM companion_batch_events "
                   "WHERE batch_id<>'' AND state='pending')",
                   (agent_id, scope_id, epoch))


def pending_events(core, *, now=None):
    """未送达、未过期、且仍在当前遗忘纪元内的事件。"""
    del now
    repo, agent_id, scope_id, epoch = _repo(core), _agent_id(core), _scope_id(core), _epoch(core)
    ensure(repo)
    _expire_forgotten(repo, agent_id, scope_id, epoch)
    rows = repo._all("SELECT * FROM companion_batch_events WHERE agent_id=? AND scope_id=? "
                     "AND state='pending' AND epoch=? ORDER BY occurred_at ASC", (agent_id, scope_id, epoch))
    return [_row_event(row) for row in rows]


# ------------------------------------------------------------------ 选批
def _last_delivered_at(core, topic):
    repo = _repo(core)
    row = repo._one('SELECT MAX(updated_at) AS at FROM companion_batches WHERE agent_id=? AND scope_id=? '
                    'AND topic=? AND state=?', (_agent_id(core), _scope_id(core), topic, 'delivered'))
    return float(row['at']) if row and row['at'] else 0.0


def _due(core, level, topic, at, limits):
    if level in (IMMEDIATE, AWAITED, STATE):
        return True
    window_key = {CONVERSATION: 'conversation_window_seconds', DAILY: 'daily_window_seconds',
                  BACKGROUND: 'background_window_seconds'}.get(level, 'state_window_seconds')
    last = _last_delivered_at(core, topic)
    if last and at - last < float(limits[window_key]):
        # 订阅命中的 6 级可以提前；订阅说「别叫我」的一律按窗口走。
        return False
    return True


def select(core, *, now=None, wake_reason='', conversation=False, purpose=None):
    """这一轮该装载哪几批。返回批次视图列表（每批内部每条事件独立）。

    装批前先按投递台账**当前**状态刷新「未确认投递」类事件：装批到送达之间那条消息
    可能已经发出去了，用旧快照会给她过期情报（2026-11-21 01:25 的重复发言事故）。
    """
    at = _now(now)
    limits = thresholds(core)
    subs = subscriptions(core)
    try:
        refresh_delivery_events(core, now=at)
    except Exception:  # noqa: BLE001 - 刷新失败仍可用旧事实，不能因此不送
        log.warning('装批前刷新投递事实失败', exc_info=True)
    events = pending_events(core, now=at)
    if not events:
        return []
    groups = {}
    for event in events:
        level = classify(event, thresholds_=limits, subscriptions=subs)
        event = dict(event, level=level)
        topic = topic_for(event, level)
        groups.setdefault((level, topic), []).append(event)
    batches = []
    for level, topic in sorted(groups, key=lambda key: (key[0], key[1])):
        if len(batches) >= int(limits['max_batches_per_turn']) and level > AWAITED:
            break
        if level == CONVERSATION and not conversation:
            continue
        if not _due(core, level, topic, at, limits):
            continue
        rows = sorted(groups[(level, topic)], key=lambda item: item['occurred_at'])
        cap = int(limits['max_events_per_batch'])
        chosen, rest = rows[:cap], rows[cap:]
        batches.append(_store_batch(core, level=level, topic=topic, events=chosen, rest=rest,
                                    at=at, wake_reason=wake_reason, limits=limits,
                                    purpose=purpose, unexpanded_reason='超过本轮上限，留到下一批'))
    return batches


def _store_batch(core, *, level, topic, events, rest, at, wake_reason, limits, purpose=None,
                 unexpanded_reason=''):
    repo, agent_id, scope_id, epoch = _repo(core), _agent_id(core), _scope_id(core), _epoch(core)
    bucket = int(at // max(1, int(limits['state_window_seconds'])))
    batch_id = stable_id('batch', agent_id, scope_id, str(level), topic, str(bucket))
    existing = repo._one('SELECT state FROM companion_batches WHERE batch_id=?', (batch_id,))
    if existing is not None and existing['state'] == 'delivered':
        # 同一个窗口里已经送过一批：**不能复用它**，否则会把已送达的行重写成 pending，
        # 台账就和实际送达对不上了。换一个 id，让新旧两批都能逐条对账。
        count = repo._one('SELECT COUNT(*) AS n FROM companion_batches WHERE agent_id=? AND scope_id=? '
                          'AND topic=? AND created_at>=?',
                          (agent_id, scope_id, topic, at - float(_window_seconds(level, limits))))
        batch_id = stable_id('batch', agent_id, scope_id, str(level), topic, str(bucket),
                             'retake', str(int((count['n'] if count else 0) or 0) + 1))
    first_at = min(event['occurred_at'] for event in events)
    last_at = max(event['occurred_at'] for event in events)
    coverage = dict(event_ids=[event['event_id'] for event in events], count=len(events),
                    from_at=first_at, to_at=last_at,
                    # 裸 epoch 读不出来：同时给可读时间，并说明用的是本机时区。
                    from_at_text=_readable(first_at), to_at_text=_readable(last_at),
                    timezone='本机时区（与 state.time 同源）',
                    window_seconds=int(_window_seconds(level, limits)),
                    topics=list(dict.fromkeys(event['topic'] or topic for event in events)))
    unexpanded = dict(count=len(rest), event_ids=[event['event_id'] for event in rest],
                      reason=(unexpanded_reason if rest else ''),
                      next_window_at=at + float(_window_seconds(level, limits)),
                      next_window_at_text=_readable(at + float(_window_seconds(level, limits))))
    context = dict(wake_reason=str(wake_reason or ''), purpose=purpose,
                   level_name=LEVEL_NAMES.get(level), timing=LEVEL_TIMING.get(level),
                   shared=_shared_context(events))
    with repo.tx():
        repo._exec('INSERT OR IGNORE INTO companion_batches'
                   '(batch_id,agent_id,scope_id,epoch,level,topic,wake_reason,coverage,unexpanded,context,'
                   'state,attempts,last_error,created_at,updated_at)'
                   ' VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (batch_id, agent_id, scope_id, epoch, int(level), topic, str(wake_reason or ''),
                    json.dumps(coverage, ensure_ascii=False), json.dumps(unexpanded, ensure_ascii=False),
                    json.dumps(context, ensure_ascii=False), 'pending', 0, '', at, at))
        # 重新合批只改还没送出去的那批；已送达的批次行永远不动，也不改 attempts。
        repo._exec("UPDATE companion_batches SET level=?,topic=?,wake_reason=?,coverage=?,unexpanded=?,"
                   "context=?,state='pending',updated_at=? WHERE batch_id=? AND state='pending'",
                   (int(level), topic, str(wake_reason or ''), json.dumps(coverage, ensure_ascii=False),
                    json.dumps(unexpanded, ensure_ascii=False), json.dumps(context, ensure_ascii=False),
                    at, batch_id))
        for event in events:
            previous = repo._one('SELECT batch_id FROM companion_batch_events WHERE event_id=?',
                                 (event['event_id'],))
            repo._exec('UPDATE companion_batch_events SET batch_id=?, level=?, topic=? '
                       'WHERE event_id=? AND state=?',
                       (batch_id, int(level), topic, event['event_id'], 'pending'))
            old = str(previous['batch_id'] or '') if previous else ''
            if old and old != batch_id:
                # 上一批没送成、事件被重新合批：旧批次空掉就删，别在「最近批次」里留空壳。
                left = repo._one("SELECT COUNT(*) AS n FROM companion_batch_events "
                                 "WHERE batch_id=? AND state='pending'", (old,))
                if not (left['n'] if left else 0):
                    repo._exec('DELETE FROM companion_batches WHERE batch_id=? AND state=?',
                               (old, 'pending'))
    return view(core, batch_id, events=events, coverage=coverage, unexpanded=unexpanded,
                context=context, level=level, topic=topic)


def _window_seconds(level, limits):
    return {CONVERSATION: limits['conversation_window_seconds'], AWAITED: AWAITED_WINDOW_SECONDS,
            STATE: limits['state_window_seconds'], DAILY: limits['daily_window_seconds'],
            BACKGROUND: limits['background_window_seconds']}.get(level, limits['state_window_seconds'])


def _shared_context(events):
    """同一批的共享背景：任务/场景/会话——只写共同点，不替每条事件编共同原因。"""
    shared = {}
    refs = [event.get('source_ref') for event in events if event.get('source_ref')]
    if len(set(refs)) == 1 and refs:
        shared['same_source_ref'] = refs[0]
    topics = {str((event.get('payload') or {}).get('task_ref') or '') for event in events}
    if len(topics) == 1 and topics != {''}:
        shared['task_ref'] = topics.pop()
    scenes = {str((event.get('payload') or {}).get('scene') or '') for event in events}
    if len(scenes) == 1 and scenes != {''}:
        shared['scene'] = scenes.pop()
    return shared


# ------------------------------------------------------------------ 视图
def view(core, batch_id, *, events=None, coverage=None, unexpanded=None, context=None,
         level=None, topic=None):
    """请求体/回执共用的批次视图。每条事件仍是自己的字典，不拼成一句话。"""
    if events is None:
        repo = _repo(core)
        rows = repo._all('SELECT * FROM companion_batch_events WHERE batch_id=? ORDER BY occurred_at ASC',
                         (batch_id,))
        events = [_row_event(row) for row in rows]
        found = repo._one('SELECT * FROM companion_batches WHERE batch_id=?', (batch_id,))
        batch = dict(found) if found is not None else {}
        coverage = coverage or json.loads(batch.get('coverage') or '{}')
        unexpanded = unexpanded or json.loads(batch.get('unexpanded') or '{}')
        context = context or json.loads(batch.get('context') or '{}')
        level = int(batch.get('level') or level or STATE)
        topic = topic or batch.get('topic') or ''
    return dict(
        batch_id=batch_id, level=int(level or STATE), level_name=LEVEL_NAMES.get(int(level or STATE)),
        topic=topic or '', coverage=coverage or {}, unexpanded=unexpanded or {},
        context={key: value for key, value in (context or {}).items() if value not in ('', None)},
        events=[dict(event_id=event.get('event_id'), source_type=event.get('source_type'),
                     source_ref=event.get('source_ref') or '',
                     action_ref=event.get('action_ref') or '',
                     at=time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(event.get('occurred_at') or 0)),
                     title=event.get('title') or '', detail=event.get('detail') or '',
                     stop_state=event.get('stop_state') or '',
                     affects_current_action=bool(event.get('affects_current_action')))
                for event in events],
        unexpanded_count=int((unexpanded or {}).get('count') or 0),
        meaning='每条事件是独立的一件；同一批只是同一窗口、同一话题，不是同类事合并成一句。')


# ------------------------------------------------------------------ 台账
def deliver(core, batches, decision_id, *, now=None):
    """模型请求成功之后才调用：把「这一批真的装进请求了」写进台账。

    只写一次：已经 delivered 的批次不重复写；台账里的 event_ids 取**实际被标成
    delivered 的那几行**，因此 deliveries、批次 coverage 与事件行三处可以对账。
    """
    repo, agent_id, scope_id, at = _repo(core), _agent_id(core), _scope_id(core), _now(now)
    ensure(repo)
    # 送达这一刻再重读一次投递状态：装批到送达之间那条消息可能已经发出去了，
    # 台账（和事件行）必须写当前事实，不能写装批时的快照。
    try:
        refresh_delivery_events(core, now=at)
    except Exception:  # noqa: BLE001 - 刷新失败不能阻止送达记账
        log.warning('送达前刷新投递事实失败', exc_info=True)
    written = []
    with repo.tx():
        for batch in batches or ():
            batch_id = batch.get('batch_id')
            if not batch_id:
                continue
            row = repo._one('SELECT state,coverage FROM companion_batches WHERE batch_id=?', (batch_id,))
            if row is not None and row['state'] == 'delivered':
                written.append(batch_id)
                continue
            ids = [r['event_id'] for r in repo._all(
                "SELECT event_id FROM companion_batch_events WHERE batch_id=? AND state='pending' "
                "ORDER BY occurred_at ASC", (batch_id,))]
            try:
                coverage = json.loads((row['coverage'] if row is not None else None) or '{}')
            except ValueError:
                coverage = {}
            coverage['delivered_event_ids'] = ids
            coverage['delivered_count'] = len(ids)
            repo._exec('INSERT OR REPLACE INTO companion_batch_deliveries'
                       '(batch_id,decision_id,agent_id,scope_id,level,event_ids,coverage,unexpanded,model_ok,'
                       'reason,at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (batch_id, str(decision_id or ''), agent_id, scope_id, int(batch.get('level') or STATE),
                        json.dumps(ids, ensure_ascii=False),
                        json.dumps(coverage, ensure_ascii=False),
                        json.dumps(batch.get('unexpanded') or {}, ensure_ascii=False), 1, '', at))
            repo._exec('UPDATE companion_batches SET state=?, coverage=?, updated_at=?, delivered_at=? '
                       'WHERE batch_id=?', ('delivered', json.dumps(coverage, ensure_ascii=False), at, at,
                                            batch_id))
            for event_id in ids:
                # 一事件一行：以后要按事件回查「她是什么时候看到这条的」，不必再拆 JSON。
                repo._exec('INSERT OR REPLACE INTO companion_batch_delivery_events'
                           '(batch_id,decision_id,event_id,agent_id,scope_id,level,at)'
                           ' VALUES(?,?,?,?,?,?,?)',
                           (batch_id, str(decision_id or ''), event_id, agent_id, scope_id,
                            int(batch.get('level') or STATE), at))
            repo._exec("UPDATE companion_batch_events SET state='delivered', delivered_at=? "
                       "WHERE batch_id=? AND state='pending'", (at, batch_id))
            written.append(batch_id)
    return written


def fail(core, batches, decision_id, *, reason='', now=None):
    """模型请求失败/被取消：只记「这次没送成」，事件仍是 pending，下一轮能续接。

    **不能**把失败标成已处理——否则这一批事实就再也不会出现在她眼前。
    """
    repo, agent_id, scope_id, at = _repo(core), _agent_id(core), _scope_id(core), _now(now)
    ensure(repo)
    with repo.tx():
        for batch in batches or ():
            batch_id = batch.get('batch_id')
            if not batch_id:
                continue
            row = repo._one('SELECT state FROM companion_batches WHERE batch_id=?', (batch_id,))
            if row is not None and row['state'] == 'delivered':
                continue  # 已送达的批次不再改 attempts，也不写第二行失败记录
            repo._exec('INSERT OR REPLACE INTO companion_batch_deliveries'
                       '(batch_id,decision_id,agent_id,scope_id,level,event_ids,coverage,unexpanded,model_ok,'
                       'reason,at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (batch_id, str(decision_id or ''), agent_id, scope_id, int(batch.get('level') or STATE),
                        json.dumps((batch.get('coverage') or {}).get('event_ids') or [], ensure_ascii=False),
                        json.dumps(batch.get('coverage') or {}, ensure_ascii=False),
                        json.dumps(batch.get('unexpanded') or {}, ensure_ascii=False), 0,
                        str(reason or '')[:200], at))
            repo._exec('UPDATE companion_batches SET attempts=attempts+1,last_error=?,updated_at=? '
                       'WHERE batch_id=?', (str(reason or '')[:200], at, batch_id))
    return None


# ------------------------------------------------------------------ 查询
def recent(core, *, limit=5, now=None):
    """最近批次（含未展开数）：给 awareness / 按需读取用。

    不返回空批：遗忘纪元前进、事件被作废之后，空掉的 pending 批次不算「最近批次」。
    """
    del now
    repo, agent_id, scope_id, epoch = _repo(core), _agent_id(core), _scope_id(core), _epoch(core)
    ensure(repo)
    _expire_forgotten(repo, agent_id, scope_id, epoch)
    rows = repo._all('SELECT * FROM companion_batches WHERE agent_id=? AND scope_id=? '
                     'ORDER BY updated_at DESC LIMIT ?',
                     (agent_id, scope_id, max(int(limit) * 3, int(limit))))
    out = []
    for row in rows:
        ids = [r['event_id'] for r in repo._all(
            'SELECT event_id FROM companion_batch_events WHERE batch_id=?', (row['batch_id'],))]
        if not ids and row['state'] != 'delivered':
            continue  # 空壳 pending 批次：不冒充「最近批次」
        out.append(dict(batch_id=row['batch_id'], level=int(row['level']),
                        level_name=LEVEL_NAMES.get(int(row['level'])), topic=row['topic'],
                        state=row['state'], wake_reason=row['wake_reason'], events=len(ids),
                        coverage=json.loads(row['coverage'] or '{}'),
                        unexpanded=json.loads(row['unexpanded'] or '{}'),
                        attempts=int(row['attempts'] or 0), last_error=row['last_error'],
                        created_at=row['created_at'], created_at_text=_readable(row['created_at']),
                        delivered_at=row['delivered_at'], delivered_at_text=_readable(row['delivered_at'])))
        if len(out) >= int(limit):
            break
    return out


def unexpanded(core, *, now=None):
    """还没送到她眼前的事件（含没装进本轮的）：按**实际级别**归类 + 计数。

    以前直接读库里那一列（没合批的事件默认是 4 级），于是所有未合批事件都被说成
    「状态变化」。现在按当前订阅/影响范围重新判级；判不出来才算 unknown，并给出
    可回查的事件 id。
    """
    del now
    repo, agent_id, scope_id, epoch = _repo(core), _agent_id(core), _scope_id(core), _epoch(core)
    ensure(repo)
    _expire_forgotten(repo, agent_id, scope_id, epoch)
    limits, subs = thresholds(core), subscriptions(core)
    rows = repo._all("SELECT * FROM companion_batch_events WHERE agent_id=? AND scope_id=? "
                     "AND state='pending' AND epoch=? ORDER BY occurred_at ASC",
                     (agent_id, scope_id, epoch))
    counts, unknown_ids = {}, []
    for row in rows:
        event = _row_event(row)
        try:
            level = classify(event, thresholds_=limits, subscriptions=subs)
        except Exception:  # noqa: BLE001 - 判不出来就归 unknown，可回查
            level = 0
        if level not in LEVEL_NAMES:
            unknown_ids.append(event['event_id'])
            continue
        counts[level] = counts.get(level, 0) + 1
    by_level = {LEVEL_NAMES[level]: count for level, count in sorted(counts.items())}
    if unknown_ids:
        by_level['unknown'] = len(unknown_ids)
    total = sum(counts.values()) + len(unknown_ids)
    out = dict(count=total, by_level=by_level,
               reason='' if not total else '还没送到你眼前；下一轮会按级别逐批送过去',
               meaning='这些事件已经发生并记录，只是还没被投递——不是没发生，也不是被丢弃')
    if unknown_ids:
        out['unknown_event_ids'] = unknown_ids[:8]
        out['unknown_meaning'] = '级别判不出来的事件（可按 event_id 回查原文），不冒充任何一个级别'
    return out


def summary(core, *, now=None, limit=5):
    return dict(levels=LEVEL_NAMES, unexpanded=unexpanded(core, now=now),
                recent=recent(core, limit=limit, now=now), subscriptions=subscriptions(core))
