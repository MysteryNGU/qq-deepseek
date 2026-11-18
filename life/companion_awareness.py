# -*- coding: utf-8 -*-
"""awareness：持续认知的只读投影与按需读取。

它回答「我在哪、用谁的号、正在关注什么、手上有几件事、手上这几件有没有新结果、
现在真能用什么」。与 ``current_state`` 的分工：``current_state`` 讲她自己的状态和
手上事项；本模块只补它没有的那一层——运行事实（节点、账号、本轮来源）、
带期限的关注、能力目录、活动任务与新结果。

三条硬规矩（与 docs/companion-implementation-plan.md §3 一致）：

1. **只读**。``build()`` 不写库、不发网络、不改 state 其他键；按需读取也不产生
   对外发言、不默认改写记忆。
2. **未知就是未知**。字段为 ``None`` / ``[]`` 表示不知道，调用方不得补造。
   账号在未登录核验时是 ``unknown``；通道没启用是 ``disabled``；这些都不会
   被写成 ``connected``。运行事实只能来自运行代码的实测，模型的输出改不了它。
3. **失败隔离**。任何一段抛错只让这一段进 ``projection_errors``，不拖垮整轮认知。
   能力目录里的 ``needs_setup`` 一定带 ``reason``；``dsh`` 在 ``strict`` 预算下
   显式拒绝并给原因，不静默从目录里消失（P0 定位的缺陷修法）。
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

PROJECTION_VERSION = 1
# 结构变化（增删必填字段、改语义）时递增；消费方据此判断能否解析旧快照。
SCHEMA_VERSION = 'awareness-1'
NODE = 'cloud-linux'

STATES = ('ready', 'needs_setup', 'offline', 'unsupported', 'unknown')
ATTENTION_MODES = ('focused', 'watching', 'muted', 'released')
ACCOUNT_STATES = ('connected', 'disconnected', 'disabled', 'unconfigured', 'login_required', 'unknown')
# 计划 B 才会接线；在那之前只能是 needs_setup，不能靠这里宣布可用。
AGENT_CAPABILITY = 'agent.subagent'
READ_OPS = ('awareness', 'people', 'conversations', 'experience', 'used',
            'attention', 'capabilities', 'agent_tasks')
SET_OPS = ('attention_set',)
NOT_IMPLEMENTED = {
    'people': '按需读取人物档案需要 C 阶段的人物模型（跨会话、跨平台合并同一人、'
              '非主人的可见边界）。本阶段只投影当前这段会话里的人，不做跨会话检索。',
    'conversations': '会话索引进 C 阶段（QQ 多人交流与 conversation_ref 路由同时落地）。'
                     '现在只有当前这段会话的 conversation_ref，没有全量会话表可列。',
    'experience': '经历时间线要按人分层的可见性策略，属 C 阶段。'
                  '本阶段只用 recent_experience 给当前会话里的几条，不做检索。'
                  '「我自己用过什么、拿到过什么回执」是另一件事，用 op=used 查。',
}

BEIJING = timezone(timedelta(hours=8))

# 子 agent 的七个操作。真源在 companion_agents.OPS，这里冗余一份只为在
# ```companion_awareness`` 单独被 import 时（测试/审计脚本）仍能给出说明。
try:
    from .companion_agents import OPS as OP_NAMES
except Exception:  # noqa: BLE001 - 单独 import 时不拖垮整份投影
    OP_NAMES = ('create', 'send', 'inspect', 'wait', 'cancel', 'resume', 'read_artifact')


# ------------------------------------------------------------------ 小工具

def _stamp(value):
    """ISO8601 秒级，带 +08:00；值本身不是时间就返回 None（不编造）。"""
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return datetime.fromtimestamp(float(value), BEIJING).isoformat(timespec='seconds')


def _parse(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value > 0 else None


def _section(name, errors, fn, default=None):
    """单段失败只让这一段未知，并留下可定位的记录，不吞掉整份投影。"""
    try:
        return fn()
    except Exception as error:  # noqa: BLE001 - 记录后继续，错误不静默
        errors.append(dict(section=name, error=type(error).__name__, detail=str(error)[:160]))
        log.warning('awareness 段 %s 失败', name, exc_info=True)
        return default


def _first(value, default=None):
    if isinstance(value, (list, tuple)):
        return value[0] if value else default
    return default if value in (None, '', {}) else value


def _fields(action):
    """op 和参数可能放在 arguments(JSON 字符串) 里，也可能在顶层。"""
    raw = action.get('arguments')
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or '{}')
        except ValueError:
            raw = {}
    merged = dict(raw) if isinstance(raw, dict) else {}
    for key in ('op', 'query', 'ref', 'cursor', 'limit', 'since', 'until',
                'actor_ref', 'conversation_ref', 'mode', 'reason',
                'review_at', 'expires_at', 'goal', 'kind', 'kinds'):
        if action.get(key) not in (None, '') and merged.get(key) in (None, ''):
            merged[key] = action[key]
    return merged


def spending_mode(policy):
    """兼容旧调用：真正的模式判定在 ``companion_agents.budget_mode``（含真实配置对象）。"""
    from .companion_agents import budget_mode
    return budget_mode(policy)





# ------------------------------------------------------------------ 分段

def _server_of(core):
    """常驻服务模式下，core 由 CompanionServer 持有；找到它才能看到通道状态。"""
    executor = getattr(core, 'executor', None)
    for _ in range(4):
        if executor is None:
            return None
        server = getattr(executor, 'server', None)
        if server is not None:
            return server
        executor = getattr(executor, 'original', None)
    return None


def _channel_facts(core, config):
    """每个通道的真实状态：登录核验过的快照优先，其次是配置文件。

    这是 ``channel.*`` 能力条目和 ``self.account`` 的**同一条来源**。
    以前 ``channel.*`` 的 ready 判据是「执行器报没报动作」，与登录无关——
    生产上 QQ 根本没有连接进程，条目却照样 ready。通道是否在线，只能问通道自己。

    返回 ``{channel: {state, enabled, source_ref, source_scope, error, observed_at}}``，
    state 取值见 ``ACCOUNT_STATES``；拿不到就是 unknown，不猜。
    """
    observed = time.time()
    try:
        from .companion_im import load_im_settings
        settings_by_channel = load_im_settings(core.home) or {}
    except Exception:
        settings_by_channel = {}
    if not settings_by_channel:
        # load_im_settings 拿不到就逐个退到各自的 loader，不因此把通道判成没配置。
        try:
            from .companion_qq import load_qq_settings
            settings_by_channel = dict(qq=load_qq_settings(core.home) or {})
        except Exception:
            settings_by_channel = {}
        try:
            from .companion_wechat import load_wechat_settings
            settings_by_channel['wechat'] = load_wechat_settings(core.home) or {}
        except Exception:
            pass
    snapshots, snapshot_error, snapshot_ref = {}, '', ''
    server = _server_of(core)
    im = getattr(server, 'im', None) if server is not None else None
    if im is None and server is not None:
        # 服务先起、IM 后起的那一小段：直接读通道管理器，问它现在的状态。
        factory = getattr(server, 'im_factory', None)
        if callable(factory):
            try:
                im = factory()
            except Exception as failure:
                snapshot_error = type(failure).__name__
    if im is not None:
        try:
            snapshots = dict((im.snapshot() or {}).get('channels') or {})
            snapshot_ref = 'life/companion_im.py#ImManager.snapshot'
        except Exception as failure:
            snapshot_error = type(failure).__name__
        for channel in ('qq', 'wechat'):
            if channel in snapshots:
                continue
            bot = getattr(im, channel, None)
            if bot is not None and hasattr(bot, 'snapshot'):
                try:
                    snapshots[channel] = bot.snapshot()
                    snapshot_ref = snapshot_ref or 'life/companion_qq.py#CompanionQQ.snapshot'
                except Exception as failure:
                    snapshot_error = snapshot_error or type(failure).__name__
    out = {}
    for channel in ('qq', 'wechat'):
        settings = settings_by_channel.get(channel) or {}
        snapshot = snapshots.get(channel)
        # 三档，按「我知道多少」从强到弱，**不许越档**：
        #   有 snapshot        → 用核验过的运行时状态（最可信）
        #   没有 IM 对象可问    → 只能读配置，那是「配置怎么说」，不是「现在怎样」
        #   问了但抛错/拿不到  → **不知道**。绝不能退回配置再断言「主动关的」：
        #        配置 enabled=false 只说明有人写过那行字，不等于此刻状态（F-3）。
        if snapshot is not None:
            state = str(snapshot.get('state') or 'unknown')
            error = str(snapshot.get('error') or '')
            # account 是登录核验过才有的值；核对前不把配置里的号当成在用。
            value = snapshot.get('account') if state == 'connected' else None
            value = str(value) if value not in (None, '', 0, '0') else None
            scope, ref = 'runtime_snapshot', snapshot_ref or 'life/companion_im.py#ImManager.snapshot'
        elif snapshot_error:
            value, error, state = None, snapshot_error, 'unknown'
            scope = 'runtime_snapshot_failed'
            ref = 'life/companion_im.py#ImManager.snapshot'
        else:
            value, error = None, ''
            if not bool(settings.get('enabled')):
                state = 'unconfigured' if not settings else 'disabled'
            elif settings.get('login_required'):
                state = 'login_required'
            else:
                state = 'unknown'
            scope = 'channel_config'
            ref = 'life/companion_%s.py#load_%s_settings' % (channel, channel)
        if state not in ACCOUNT_STATES:
            state = 'unknown'
        out[channel] = dict(value=value, state=state, enabled=bool(settings.get('enabled')),
                            error=(error or None), source_ref=ref, source_scope=scope,
                            observed_at=_stamp(observed))
    return out


def _self_account(core, config):
    """账号事实只来自登录核验，不来自 persona，也不来自 cfg.bot_qq 的存在与否。

    三处来源，从新到旧：ImManager/CompanionQQ 的 snapshot、进程持有的 qq 对象、
    通道配置文件。任何一处都拿不到就是 unknown；通道没启用是 disabled。
    """
    facts = _channel_facts(core, config)
    qq = dict(facts.get('qq') or {})
    return dict(value=qq.get('value'), value_visibility='owner-only', state=qq.get('state'),
                enabled=qq.get('enabled'), error=qq.get('error'),
                source_ref=qq.get('source_ref'), source_scope=qq.get('source_scope'),
                observed_at=qq.get('observed_at'))


def _self_agent(core):
    agent_id = getattr(getattr(core, 'life', None), 'agent_id', None)
    return dict(agent_id=str(agent_id) if agent_id else None,
                account_ref='account:' + str(agent_id or 'unknown'),
                source_ref='life/runtime.py#LifeRuntime.agent_id',
                observed_at=_stamp(time.time()))


def _runtime(core, state):
    home = getattr(core, 'home', None)
    available = state.get('available_actions')
    return dict(
        node=NODE,
        home=str(home) if home is not None else None,
        available_actions=list(available) if isinstance(available, (list, tuple)) else None,
        spending_mode=spending_mode(getattr(getattr(core, 'spending', None), 'policy', None)),
        # 进程事实：启动时间与跨重启寿命；不是她「醒来」的时间。
        process_started_at=_stamp(getattr(core, 'process_started_at', None)),
        started_at=_stamp(getattr(core, 'started_at', None)),
        autonomy_active=bool(getattr(core, 'active', True)),
        observed_at=_stamp(time.time()),
        source_ref='life/companion.py#Companion.__init__',
    )


def _here(core, current, wake_kind):
    scope = getattr(core, 'scope', None)
    owner = getattr(getattr(core, 'cfg', None), 'owner', None)
    source = None
    if current is not None:
        payload = getattr(current, 'payload', None) or {}
        source = payload.get('input_source') or payload.get('source') or None
        if isinstance(source, str):
            source = dict(kind=source)
    observed = getattr(current, 'received_at', None) or getattr(current, 'created', None) or time.time()
    participant = getattr(scope, 'participant_id', None)
    platform = None
    if isinstance(source, dict):
        platform = source.get('kind')
    scope_ref = getattr(scope, 'scope_id', None)
    conversation = getattr(scope, 'conversation_id', None)
    agent_id = getattr(getattr(core, 'life', None), 'agent_id', None)
    return dict(
        # 三个引用各有各的对象，不混用：当前说话的人 / 登录的号 / 这段会话。
        actor_ref=('actor:' + str(participant)) if participant else None,
        account_ref='account:' + str(agent_id) if agent_id else None,
        conversation_ref=('conversation:' + str(conversation)) if conversation else None,
        scope_ref=scope_ref,
        owner_ref=('actor:' + str(owner)) if owner not in (None, '') else None,
        platform=platform,
        input_source=source if isinstance(source, dict) else None,
        wake_kind=wake_kind,
        observed_at=_stamp(observed),
        # 当前的人与会话来自这段会话的持久作用域；平台的在线与否由 presence 段给。
        source_ref='life/runtime.py#LifeRuntime.scope',
    )


def _attention(core, attention, now):
    stored = None
    errors = []
    projected = _section('attention_stored', errors,
                         lambda: core._state('companion_attention') or [])
    out = []
    for row in projected if isinstance(projected, list) else []:
        if not isinstance(row, dict) or row.get('mode') not in ATTENTION_MODES:
            continue
        expires = _parse(row.get('expires_at'))
        if expires is not None and now >= expires:
            # 到期的关注不再是当前关注：抹平为 released，并留下 fact。
            out.append(dict(conversation_ref=row.get('conversation_ref'), mode='released',
                            reason=str(row.get('reason') or '到达过期时间'),
                            since=_stamp(row.get('since')), review_at=_stamp(row.get('review_at')),
                            expires_at=_stamp(row.get('expires_at')), cursor=row.get('cursor'),
                            stale=True, source_ref='meta:companion_attention',
                            observed_at=_stamp(now)))
            continue
        out.append(dict(conversation_ref=row.get('conversation_ref'), mode=row['mode'],
                        reason=str(row.get('reason') or ''), since=_stamp(row.get('since')),
                        review_at=_stamp(row.get('review_at')),
                        expires_at=_stamp(row.get('expires_at')), cursor=row.get('cursor'),
                        source_ref='meta:companion_attention', observed_at=_stamp(now)))
    for row in attention or []:
        if not isinstance(row, dict):
            continue
        conv = row.get('conversation_ref') or row.get('conversation') or row.get('conversation_id')
        if not conv or any(x.get('conversation_ref') == conv for x in out):
            continue
        out.append(dict(conversation_ref=conv, mode='focused', reason='本轮注意力把它排在最前',
                        since=_stamp(row.get('since') or now), review_at=_stamp(row.get('review_at')),
                        expires_at=_stamp(row.get('expires_at')), cursor=row.get('cursor'),
                        source_ref='life/companion_motivation.py#attention', observed_at=_stamp(now)))
    return out[:8]


def _recent_experience(core, current, now):
    rows = []
    if current is not None:
        text = str(getattr(current, 'text', '') or '').strip()
        if text:
            rows.append(dict(source_ref=str(getattr(current, 'id', '') or ''), kind='message',
                             role='user', at=_stamp(getattr(current, 'received_at', None)),
                             preview=text[:160], observed_at=_stamp(now)))
    for item in (core._recent(limit=8, include_images=False) or [])[-4:]:
        if current is not None and item.get('id') == getattr(current, 'id', None):
            continue
        text = str(item.get('content') or '').strip()
        if not text:
            continue
        rows.append(dict(source_ref=str(item.get('id') or ''), kind='message',
                         role=item.get('role'), at=_stamp(item.get('created')),
                         preview=text[:160], observed_at=_stamp(now)))
    rows.sort(key=lambda row: row.get('at') or '')
    return rows[-5:]


def _capability(capability_ref, category, description, status, *, reason=None,
                evidence=None, checked_at=None, schema_ref=None, node=NODE):
    if status not in STATES:
        raise ValueError('unknown capability status')
    if status == 'needs_setup' and not reason:
        raise ValueError('needs_setup 必须带 reason')
    return dict(capability_ref=capability_ref, category=category, description=description,
                node=node, status=status, reason=reason, evidence=evidence or [],
                checked_at=checked_at, schema_ref=schema_ref)


def _dsh_endpoint(executor):
    """沿执行器链找到真正持有 DSH 配置的那一层；拿不到就是未知（None）。

    三态：True（可用）/ False（探测到不可用）/ None（读不到）。

    **这份实现有一份拷贝在 `life/companion_world.py::_dsh_endpoint_probe`。**
    事实层不能反向 import 本模块（会成环），所以复制；两边都留了指针，
    并有测试断言行为一致。**改一边必须改另一边。**
    """
    seen = 0
    while executor is not None and seen < 4:
        paths = getattr(executor, '_dsh_paths', None)
        if callable(paths):
            try:
                return paths() is not None
            except Exception:
                return None
        executor = getattr(executor, 'original', None)
        seen += 1
    return None


def _guide(kind):
    """这个 kind 的用法说明（companion_context.GUIDES 里那一段）。"""
    try:
        from .companion_context import GUIDES
        return GUIDES.get(str(kind))
    except Exception:
        return None


def _guide_tail(kind):
    """用法说明的「参数要点」一行：GUIDES 开头那句 op/参数约定。

    只有一句、到第一个句号为止——主上下文里一句话一条，完整段落仍由
    ``tool_instructions(kinds)`` 注入，细节走 memory op=capabilities。
    """
    text = _guide(kind)
    if not text:
        return None
    text = str(text).strip()
    for stop in ('。', '；'):
        if stop in text:
            text = text.split(stop, 1)[0]
            break
    # 再硬截一道：GUIDES 的第一句常常很长（web/phone 一整段才一个句号），
    # 摘要里一句话一条就够了，完整段落本来就在 Instructions 里。
    return (text[:48] + '…') if len(text) > 48 else text


def _action_description(kind):
    """能力条目的说明分两层：管理员口径 + 模型口径。

    description 保持旧形状（``动作种类 <kind>``）不动，下游有按它匹配的地方；
    模型读的那一层放在 ``what``，它直接来自 GUIDES，不靠模型记忆。
    """
    return dict(capability_ref=None, category='action', description='动作种类 ' + str(kind),
                what=_guide_tail(kind), how_ref='companion_context.GUIDES[' + str(kind) + ']')


def _action_entry(kind, status, *, reason=None, evidence=None, checked_at=None, scope=None,
                  availability=None):
    """动作种类的能力条目：状态、原因、依据、怎么用，四个都在。

    与 ``_capability`` 的差别只在多带了 ``what``（取自 GUIDES）与 ``how_ref``，
    目的是让「能不能用」和「怎么用」两条通路共用同一个条目，不再各走各的。
    """
    item = _capability('action.' + str(kind), 'action', '动作种类 ' + str(kind), status,
                       reason=reason, evidence=evidence, checked_at=checked_at,
                       schema_ref='life/companion.py#DECISION.actions')
    item['what'] = _guide_tail(kind)
    item['how_ref'] = 'companion_context.GUIDES[' + str(kind) + ']' if _guide(kind) else None
    if scope:
        item['state_scope'] = scope
    if availability:
        item['availability'] = availability
    return item


def _probe(fn, *args):
    """跑一次探测，返回 (值, 失败原因)。**失败不是结果**，调用方必须区别对待。

    这是 F-2/F-3 的根因统一处理：探测抛错时，某些分支曾经写成
    「本机没有手机控制通道」「通道被关了」这种**断言**——但我们其实什么都不知道。
    所有探测都走这里，失败一律 ``(None, 异常名)``，由调用方报 unknown + 说「读不到」。
    """
    try:
        return fn(*args), None
    except Exception as failure:  # noqa: BLE001 - 探测失败要变成「不知道」，不是崩
        return None, type(failure).__name__


def _unknown_reason(what, detail=''):
    """统一口径的「不知道」：只说我这边看到什么、不知道什么，绝不断言。"""
    head = '我读不到%s的状态' % what
    if detail:
        head += '（%s）' % detail
    return head + '，所以现在不能确认它能不能用；这不能说明它坏了，也不能说明它没有。'


def _world():
    """惰性取事实层模块：避免模块加载期形成循环依赖。"""
    from . import companion_world
    return companion_world


def _world_fact(fact_ref, build):
    """取一条事实；构建失败就退回 unknown（不让事实层的问题拖垮能力判断）。"""
    try:
        value = build()
        if isinstance(value, dict) and value.get('fact_ref'):
            return value
        return _world().unknown(fact_ref.split('.')[0], '事实层没有返回这条事实',
                                source_ref='life/companion_world.py', fact_ref=fact_ref)
    except Exception as error:  # noqa: BLE001 - 事实层异常要降级成 unknown，不能抛给调用方
        return _world().unknown(fact_ref.split('.')[0], '%s: %s' % (type(error).__name__, error),
                                source_ref='life/companion_world.py', fact_ref=fact_ref)


def _world_detail(fact_ref, raw, *, fallback):
    """只取事实的 detail（手机那两处用），构建不出来就用 fallback。"""
    try:
        value = _world().phone_context_fact(raw)
        if isinstance(value, dict) and value.get('fact_ref') == fact_ref:
            return str(value.get('detail') or fallback)
    except Exception:  # noqa: BLE001
        pass
    return fallback


def _offline_reason(core, kind, *, executor):
    """这个 kind 现在不可用的具体原因。**不知道就说不确定，不编原因**。

    三档必须分清（F-2 的统一修法）：
      - 探测成功且明确不可用 → 说具体原因（offline / needs_setup）
      - 探测抛错 / 拿不到     → unknown + 「读不到」，**不许写成断言**
      - 探测说「能用」         → 这里不该被调到；真发生了就如实说没有可靠原因
    """
    if kind == 'phone':
        context, failure = _probe(lambda: executor.phone_context() if hasattr(executor, 'phone_context')
                                  else None)
        if failure is not None:
            return _unknown_reason('手机通道', '探测抛错 %s' % failure), 'unknown'
        if context is None:
            # 执行器没给 phone_context：读不到状态，不等于"这台机器没有手机能力"。
            return _unknown_reason('手机通道', '执行器没给 phone_context'), 'unknown'
        devices = context.get('devices') or []
        if not devices:
            # 事实由事实层统一生成（含状态、来源、观察时间），这里只追加建议。
            # 文案里不再写死秒数：注册窗口是 `companion_phone.RECENT_SECONDS`，
            # 常量一改，写死的数字就会撒谎。精确值在 `raw` 里，需要时按需读取。
            detail = _world_detail('phone.link', context, fallback='手机最近没有上报过注册，看不到在线设备')
            return detail + '；这不表示我没有操作手机的能力。' \
                            '要让它恢复，在手机上打开小鱼 App 并保持前台即可。', 'offline'
        if any(item.get('has_stream') for item in devices):
            return ('手机注册过但操作拿不到回执（可能被系统挂起或 App 在后台），'
                    '所以现在派活送不到；把 App 切回前台再试。'), 'offline'
        return ('手机上装了 App 也注册过，但没有活动连接（App 不在前台或已断开）；'
                '即便派活也送不到，需要把 App 切回前台。'), 'offline'
    if kind == 'computer':
        # peer 是桌面客户端真实连没连；读不到就说不知道，不说"没连上"。
        # 注意：这里问的是「能不能看屏幕/点鼠标」，和 body（小人做动作）是两条链路。
        peer, failure = _probe(lambda: getattr(_server_of(core), 'peer', None))
        if failure is not None:
            return _unknown_reason('桌面连接', '探测抛错 %s' % failure), 'unknown'
        fact = _world_fact('computer.link', lambda: _world().computer_fact(peer))
        if fact['state'] == 'unknown':
            return (fact['detail'].replace('读不到，不能确认：', '') or '桌面连接状态读不到'), 'unknown'
        return fact['detail'], fact['state']
    if kind == 'body':
        # body 走 `_body_context`（桌面桥推来的），**不是** server peer。
        # 两者混用会把「看不到屏幕」说成「小人不能动」，反之亦然。
        context, failure = _probe(lambda: getattr(core, '_body_context', None))
        if failure is not None:
            return _unknown_reason('桌面身体', '探测抛错 %s' % failure), 'unknown'
        fact = _world_fact('body.link', lambda: _world().body_fact(context))
        if fact['state'] == 'unknown':
            return (fact['detail'].replace('读不到，不能确认：', '') or '桌面身体状态读不到'), 'unknown'
        return fact['detail'], fact['state']
    if kind == 'town':
        # 读 TownBridge 的真实状态（`core.town.state`），不再断言「没启用或没接入码」。
        # 2026-11-18：小镇跑了 199 次动作却一直报 needs_setup——判据没读数据源。
        fact = _world_fact('town.link', lambda: _world().town_fact(getattr(core, 'town', None)))
        if fact['state'] == 'unknown':
            return (fact['detail'].replace('读不到，不能确认：', '') or '小镇状态读不到'), 'unknown'
        if fact['state'] == 'ready':
            return '小镇现在是连着的。', 'ready'
        return fact['detail'], fact['state']
    if kind == 'bilibili':
        # 三种原因分开说（插件没装 / 没登录 / 掉线），不再抹平成一句「没登录态」。
        # `_bilibili` 挂在真正持有执行能力的那一层——服务模式下是 `RemoteExecutor.original`，
        # 本地模式下就是 `ActionExecutor` 自己。
        backend = None
        node = executor
        for _ in range(4):
            if node is None:
                break
            backend = getattr(node, '_bilibili', None)
            if backend is not None:
                break
            node = getattr(node, 'original', None)
        if backend is None:
            # 执行器上没有 B站后端：这是「读不到」，不是「没登录」。
            return _unknown_reason('B站', '执行器上没有 bilibili 后端'), 'unknown'
        fact = _world_fact('bilibili.status', lambda: _world().bilibili_fact(backend.status()))
        if fact['state'] == 'unknown':
            return (fact['detail'].replace('读不到，不能确认：', '') or 'B站状态读不到'), 'unknown'
        if fact['state'] == 'ready':
            return 'B站后端可用。', 'ready'
        return fact['detail'], fact['state']
    if kind == 'dsh':
        # 三态必须分开：None（读不到）≠ False（本机确实没有）。
        fact = _world_fact('dsh.runtime', lambda: _world().dsh_fact(executor))
        if fact['state'] == 'unknown':
            return (fact['detail'].replace('读不到，不能确认：', '') or 'DSH 状态读不到'), 'unknown'
        if fact['state'] == 'ready':
            return '本机有可用的 DSH 运行时。', 'ready'
        return fact['detail'], fact['state']
    return '执行器没有把它报成可用；具体原因我这边看不到。', 'unknown'


# 「已实现、但现在可能不在 available_actions 里」的 kind 全集。这不是能力清单，
# 是「掉线时要留一条带原因的条目」的登记表：真正的可用性永远来自执行器。
# 掉线就整个消失，模型看到的是一个沉默的缺口——她没法解释「为什么没有手机了」。
TRACKED_KINDS = ('web', 'command', 'work', 'memory', 'growth', 'skill', 'dsh', 'agent',
                 'computer', 'phone', 'body', 'town', 'bilibili')

# 主上下文里「当前不可用」写几条。其余只列名字指向按需读取：完整原因留在条目里，
# 不为省事而删——但每轮请求体只放这么多（test_projection_stays_small 的 4000 字节上限）。
UNUSABLE_IN_PROMPT = 3

# **必须**进主上下文的条目：用户会直接问到、答错会误导的那些。
# 它们不受 UNUSABLE_IN_PROMPT 截断影响，永远各占一个位置。
# 依据 2026-11-18 对照实测：`guard.watchdog` 排在不可用项第 9 位被截掉，
# 结果她答「改坏了谁救」时一次都没引用它——条目内容完全正确，只是没送到眼前。
PINNED_REFS = ('guard.watchdog', 'action.phone', 'action.computer', 'agent.subagent')


def offline_reasons(core, kinds):
    """当轮不可用、但**能说出具体原因**的 kind：``[{kind, reason, state_scope}]``。

    与 ``capabilities()`` 用同一份 ``_offline_reason``，所以请求体里的那句
    「手机：没有活动连接」和 memory op=capabilities 读到的 reason 是同一句话。
    只返回真知道原因的；拿不到原因的不编。
    """
    present = set(kinds or ())
    executor = getattr(core, 'executor', None)
    rows = []
    for kind in TRACKED_KINDS:
        if kind in present or kind in ('dsh', 'agent'):
            continue
        reason, scope = _offline_reason(core, kind, executor=executor)
        if reason and scope != 'unknown':
            rows.append(dict(kind=kind, reason=reason, state_scope=scope))
    return rows


def _systemctl(*args):
    """跑一次 systemctl，拿回一行文本；跑不了就是 None（不猜状态）。"""
    try:
        import subprocess
        done = subprocess.run(['systemctl', *args], capture_output=True, text=True, timeout=5)
        return (done.stdout or '').strip() or None
    except Exception:  # noqa: BLE001 - 没有 systemd、超时、权限都可能，一律当未知
        return None


def watchdog_capability(core, *, now=None):
    """``guard.watchdog``：谁能把改坏的源码换回去。

    基线里她答「这台机器上没有人替我兜底」，而当时 watchdog 正 enabled+active——
    系统从没告诉过她。状态**只以 ``systemctl`` 实测为准**，不读文档（文档里
    「默认 disabled，等授权」已经过时）。跑不了 systemctl 就是 unknown，不猜。

    摘要层只放一句「现在管不管事」；what_it_does / limits 这些细节走
    ``memory op=capabilities`` 读整条。
    """
    now = time.time() if now is None else float(now)
    unit = 'companion-watchdog.service'
    active = _systemctl('is-active', unit)
    enabled = _systemctl('is-enabled', unit)
    if active is None and enabled is None:
        status, reason = 'unknown', ('这台机器上没有 systemd（或读不到它的状态），'
                                     '所以我不知道有没有人替我兜底；不能断言有，也不能断言没有。')
        scope = 'unknown'
    elif active == 'active':
        status, reason = 'ready', None
        scope = 'active'
    elif active == 'inactive':
        status = 'offline'
        reason = ('这个守护停了（当前 inactive），改坏了不会自动换回去，要人手动处理。')
        scope = 'inactive'
    else:
        status = 'offline'
        reason = ('这个守护现在是 %s，不在管事；改坏了不会自动换回去。' % (active or 'unknown'))
        scope = str(active or 'unknown')
    # 以下只在按需读取时给，不进主上下文摘要。
    # **必须随状态变化**（t7 R-03）：watchdog 停着的时候，如果这里照抄「会回滚」，
    # 用户按需读到的是"我有保障"，而实际改坏了没人管——这正是今晚反复犯的错：
    # 该说「现在不生效」的时候，不能给出让人误以为有保障的表述。
    if status == 'ready':
        what_it_does = ('重启后盯一段时间，起不来就换回上次能跑的源码版本并重新拉起，'
                        '再给我留一条消息说刚才那次改动被丢弃了。')
        when_it_acts = '只在我重启之后；正常运行期间它不动我。'
        not_do = '不动 life.db、config.toml、人格文件——数据它一概不碰。'
        limits = '只管源码快照，观察窗约一分钟；连续失败 3 次就停手等人处理，不再自动回滚。'
    elif status == 'offline':
        # 它能做这些事，但**现在没在做**——先说现状，再说能力，避免只读半句就误解。
        what_it_does = ('**现在没有在做事**：这个守护没在运行（%s）。'
                        '它本来能做的事是：我重启后盯一段时间，起不来就换回上次能跑的源码版本。'
                        '此刻改坏了源码，它不会替我做任何事，要人手动恢复。'
                        % (scope if scope != 'unknown' else '状态不明'))
        when_it_acts = '它停着，所以任何时候都不会动——包括我改坏之后。'
        not_do = '不动 life.db、config.toml、人格文件——这一条现在也成立，但前提是它没在跑。'
        limits = ('它不运行，所以没有观察窗、也没有自动回滚次数可谈：'
                  '在它被重新启用之前，源码改动全部没有兜底。')
    else:
        # 探测失败：不能说确定话，也不能给出任何"会回滚"的保证。
        what_it_does = ('我读不到它的状态，所以**不能确认它会不会在我改坏之后把源码换回去**。'
                        '它设计上负责这件事，但此刻是否在跑、是否还能接管，我这边无从得知；'
                        '按最坏情况理解：不确定有兜底，就当没有。')
        when_it_acts = '不知道。读不到它的运行状态，就无法判断它什么时候会动。'
        not_do = '不知道它现在会不会动数据——这条我无法确认，所以不能说它一定不碰。'
        limits = '读不到状态，观察窗、失败次数这些限制我也无法确认。'
    return dict(capability_ref='guard.watchdog', category='guard', node=NODE, status=status,
                description='改坏源码时的自动回滚',
                reason=reason, checked_at=_stamp(now), state_scope=scope,
                schema_ref='ops/companion-watchdog.py#DEFAULTS',
                evidence=['systemctl is-active ' + unit + '=' + str(active),
                          'systemctl is-enabled ' + unit + '=' + str(enabled)],
                source_ref='ops/companion-watchdog.py', who_owns_it='主人（用 systemctl 启停）',
                # 摘要那一句：她只需要知道「现在有没有人兜底」。
                what=('我改坏起不来时会换回上次能跑的版本' if status == 'ready'
                      else '我不知道有没有人替我兜底' if status == 'unknown'
                      else '现在没人替我兜底'),
                what_it_does=what_it_does,
                when_it_acts=when_it_acts,
                what_it_does_not_do=not_do,
                limits=limits,
                enabled=enabled, active=active)


    """当轮不可用、但**能说出具体原因**的 kind：``[{kind, reason, state_scope}]``。

    与 ``capabilities()`` 用同一份 ``_offline_reason``，所以请求体里的那句
    「手机：没有活动连接」和 memory op=capabilities 读到的 reason 是同一句话。
    只返回真知道原因的；拿不到原因的不编。
    """
    present = set(kinds or ())
    executor = getattr(core, 'executor', None)
    rows = []
    for kind in TRACKED_KINDS:
        if kind in present or kind in ('dsh', 'agent'):
            continue
        reason, scope = _offline_reason(core, kind, executor=executor)
        if reason and scope != 'unknown':
            rows.append(dict(kind=kind, reason=reason, state_scope=scope))
    return rows


def _dispatch_caveat(core, kind, *, executor):
    """``(dispatchable, caveat)``：这个在清单里按着用的 kind，**派活会不会被拒**。

    只对「状态宽、派发严」的连接类有意义（今天只有 phone）：``phone_context.connected``
    看的是 90 秒注册窗口，而 ``run()`` 要求真有活动连接。**这不是改状态，是补一句
    提醒**——status 仍如实反映执行器的判断，caveat 说明落差（F-4）。
    """
    if kind != 'phone':
        return True, None
    context, failure = _probe(lambda: executor.phone_context() if hasattr(executor, 'phone_context')
                              else None)
    if failure is not None or context is None:
        # 读不到就不断言能派活，也不断言不能。
        return None, _unknown_reason('手机通道', failure and ('探测抛错 %s' % failure) or '没有 phone_context')
    devices = context.get('devices') or []
    if any(item.get('has_stream') for item in devices):
        return True, None
    if not devices:
        # 清单说能用，但我这边看不到任何在线设备：这句必须跟事实走，
        # 不能照抄「注册过但没有活动连接」——那是另一种情形。
        return False, ('执行器把手机报成了可用，但我这边看不到任何在线设备；'
                       '现在派活很可能被拒，先确认手机上小鱼 App 在前台再操作。')
    return False, (_world_detail('phone.link', context,
                                 fallback='手机注册过但没有活动连接')
                   + '；现在派活会被拒（需要 App 在前台）。先让它回到前台再操作。')


def capabilities(core, state=None, *, now=None):
    """能力目录：每条都能落到「谁在用、凭什么说它能用、怎么用」。

    ready 只给执行器当轮真的报成可用的动作种类；``available_actions`` 里有它，
    就是执行器这一轮探测过的事实。**没在里面的 kind 不再静默消失**：照 dsh 的
    写法补一条 ``needs_setup``/``offline`` 条目并写清原因（TRACKED_KINDS）。
    """
    now = time.time() if now is None else float(now)
    state = state or {}
    checked = _stamp(now)
    catalogue = []
    executor = getattr(core, 'executor', None)
    # 键 'agent' 也是动作种类（kind=agent），但它的能力条目是下面的
    # agent.subagent，不能再按「动作种类」重复登记成一条 ready。
    kinds = [kind for kind in (state.get('available_actions') or []) if kind != 'agent']
    # dsh 的条目由下面对应的那条统一登记（带 strict 原因），这里先跳过它，
    # 否则会多出一条「动作种类 ready」和一条「strict 下 needs_setup」自相矛盾。
    for kind in [kind for kind in kinds if kind != 'dsh']:
        item = _action_entry(kind, 'ready', evidence=['available_actions'], checked_at=checked)
        # 「在清单里」只说明执行器这一轮认为它可用；连接类还有一个更严的门槛：
        # 手机注册着但没活动连接时，available_kinds 仍报 phone 可用，而 run() 会拒绝
        # （t1 记的「连着≠能派活」）。这里把这个落差显式说出来，**不改 status**
        # ——status 是执行器的判断，如实反映；dispatchable 是我们对「派活会不会被拒」
        # 的诚实提示（F-4）。
        dispatchable, caveat = _dispatch_caveat(core, kind, executor=executor)
        item['dispatchable'] = dispatchable
        if caveat:
            item['reason'] = caveat
        catalogue.append(item)
    # 掉线的照样留条目：能力不在不等于这件事不存在。原因按 kind 具体说，
    # 不能说成「配置问题」糊过去——她要能回答「手机怎么连不上了」。
    for kind in TRACKED_KINDS:
        if kind in kinds or kind in ('dsh', 'agent'):
            continue
        reason, scope = _offline_reason(core, kind, executor=executor)
        item = _action_entry(kind, scope if scope in STATES else 'unknown',
                             reason=reason, evidence=['available_actions:absent'],
                             checked_at=checked, scope=scope)
        # 不在清单里 = 执行器这轮没报它可用，所以谈不上派活：
        # 显式 False（不是缺键），下游按 `dispatchable is False` 判断不会落空。
        item['dispatchable'] = False
        catalogue.append(item)
    mode = spending_mode(getattr(getattr(core, 'spending', None), 'policy', None))
    # DSH 是否存在：看真正跑 dsh 的执行器链（服务模式下是 RemoteExecutor.original）。
    executor = getattr(core, 'executor', None)
    node = NODE
    endpoint = _dsh_endpoint(executor)
    dsh_reason = None
    # 三态：None（读不到）≠ False（本机确实没有）。以前这里 `if endpoint` 把两者都判成
    # needs_setup，把「不知道」说成了「没有」——和 `_offline_reason` 的 dsh 分支同一个病。
    if endpoint is None:
        dsh_status = 'unknown'
        dsh_reason = '读不到 dsh 路径探测结果，所以不能确认本机有没有 DSH 运行时。'
    else:
        dsh_status = 'ready' if endpoint else 'needs_setup'
    # dsh 的动作种类条目由下面这条统一登记（带 strict 原因），这里不再重复。
    kinds = [kind for kind in kinds if kind != 'dsh']
    if endpoint and mode == 'strict':
        dsh_status = 'needs_setup'
        dsh_reason = ('预算模式为 strict：DSH 只暴露 4 个 RPC、没有逐次 usage，'
                      '完整计费不可知，因此本轮拒绝执行（不是静默隐藏）。'
                      '要么改 bounded_estimate，要么接受用量记为未知。')
    elif not endpoint:
        dsh_reason = '本机没有可用的 DSH 运行时（dsh_root/dsh_node 或入口缺失）。'
    dsh_item = _capability(
        'action.dsh', 'action', '把一整件多步工作交给后端代理', dsh_status,
        reason=dsh_reason, evidence=['cfg.dsh_root'] if endpoint else [],
        checked_at=checked, schema_ref='life/companion.py#kind:dsh', node=node)
    dsh_item['dispatchable'] = dsh_status == 'ready'
    catalogue.append(dsh_item)
    # 子 agent 能力：计划 B 已接线（life/companion_agents.py）。ready 只在台账里真有过
    # 一次「跑完并核验通过」时才给；否则写 needs_setup 并说清缺的是哪一步。
    catalogue.append(agent_capability(core, mode=mode, endpoint=endpoint, now=now))
    # 通道状态只问通道自己：登录核验过的 snapshot 优先，其次是配置。
    # 以前这里看「执行器报没报可用动作」，所以生产上没有 QQ 连接进程时
    # channel.qq 依旧 ready——那条判据与 QQ 登录毫无关系。
    facts = _channel_facts(core, getattr(core, 'cfg', None))
    channel_reason = {
        'connected': None,
        'disabled': '通道被关闭了（配置里 enabled=false），不是掉线，是主动关的。',
        'unconfigured': '还没有配置过这个通道。',
        'login_required': '配置好了但还没登录成功，需要重新扫码/登录。',
        'disconnected': '登录过，但现在的连接不在了。',
        'unknown': '连不上通道自己，拿不到登录状态；这是不知道，不是不在线。',
    }
    for channel in ('qq', 'wechat'):
        fact = facts.get(channel) or {}
        state_ = str(fact.get('state') or 'unknown')
        # 状态词统一来自事实层（`companion_world.channel_fact`），不再在这里另写一套：
        # 以前这里判 `disabled` → offline，而 reason 文案说「不是掉线，是主动关的」——
        # 判定和措辞用的不是同一份事实，正是今晚反复出现的那个根因。
        world_fact = _world_fact('channel.' + channel,
                                 lambda f=fact, c=channel: _world().channel_fact(c, f))
        status_ = str(world_fact.get('state') or 'unknown')
        item = _capability(
            'channel.' + channel, 'channel', '消息通道：' + channel, status_,
            reason=None if state_ == 'connected' else channel_reason.get(state_, '状态未知。'),
            evidence=['self.account:' + state_], checked_at=fact.get('observed_at') or checked,
            schema_ref='life/companion_im.py#ImManager')
        item['source_ref'] = fact.get('source_ref')
        item['source_scope'] = fact.get('source_scope')
        # state_scope 保持原义（就是通道状态本身），**不改格式**——t5 的用例按它
        # 精确比期望值，改形状会打乱下游。来源档位另给一个键，需要分辨
        # 「不知道」和「被关了」的人读 state_source 即可（t5 提的第二条）。
        item['state_scope'] = state_
        item['state_source'] = fact.get('source_scope') or 'unknown'
        # 通道不是动作 kind，不适用「派活会不会被拒」；显式给 None，
        # 免得下游按 `dispatchable is False` 判断时把「没有这个键」当成未知（t5 提的第三条）。
        item['dispatchable'] = None
        # usable 是「现在真能往外发消息吗」：通道在 **且** 账号登录过才算。
        # 只给 channel.qq: ready 容易被读成「我能用 QQ」，所以把这两件事分开写。
        item['usable'] = state_ == 'connected'
        item['what'] = '消息通道（%s）' % (
            '已登录，能收发' if state_ == 'connected' else '现在发不出消息：' + (
                channel_reason.get(state_) or '状态未知')[:24])
        catalogue.append(item)
    # 改坏了自己能不能被救回来：这件事也只以 systemctl 实测为准。
    catalogue.append(_section('guard.watchdog', [], lambda: watchdog_capability(core, now=now), None)
                     or _capability('guard.watchdog', 'guard', '改坏源码时的自动回滚',
                                    'unknown', reason='读不到守护服务的状态，不知道它管不管事。',
                                    checked_at=checked, schema_ref='ops/companion-watchdog.py'))
    # 最后归一：**每条都带 dispatchable 键**，免得下游被「缺键」当成未知（t5 提的第三条）。
    # 取值的语义必须互斥、不能靠 falsy 一锅端（t5 留给 t7 的那条）：
    #   True  → 在清单里，派活会被接收
    #   False → 在清单里但派活会被拒；或不在清单里（谈不上派活）
    #   None  → **不适用**（通道、守护这类不是动作 kind），不是「不能派活」
    # 用 `if not dispatchable` 判断会把 None 和 False 混成一种，所以这里不靠注释约束：
    # 键名就是为了「能派活吗」这个提问，不适用时给 None 是明确的「这个问题不成立」。
    # 下游要区分就写 `is False`（不能派活）/ `is None`（不适用），别用 `not`。
    for item in catalogue:
        item.setdefault('dispatchable', None)
    return catalogue


def agent_capability(core, *, mode, endpoint, now=None):
    """子 agent 能力条目：状态来自台账证据，不来自「代码写了没」。

    - ``ready``：台账里至少有一条 ``completed`` 且 ``verification='passed'`` 的记录。
    - ``strict`` + DSH：DSH 拿不到逐次用量，strict 下只能走本机命令，所以是
      ``needs_setup`` 并写原因（不是静默隐藏）。
    - 其它：``needs_setup``，原因分「还没有成功记录」和「本机没有 DSH」。
    """
    now = time.time() if now is None else float(now)
    checked = _stamp(now)
    node = NODE
    evidence_refs, reason = [], None
    repo = getattr(core, 'repo', None)
    agent_id = getattr(getattr(core, 'life', None), 'agent_id', None)
    # 「能力已接通」= 这条链路最近有一次真实的成功调用；「某次任务成果验证通过」是另一件事。
    # 这两件事分开看：能力只看调用证据，不看某件活有没有产出 artifacts、也不改它的 verification。
    done = None
    if repo is not None:
        try:
            row = repo._one("SELECT id,verification FROM companion_agent_tasks WHERE agent=? "
                            "AND status='completed' ORDER BY updated DESC LIMIT 1", (agent_id,))
            done = dict(row) if row else None
        except Exception:  # noqa: BLE001 - 台账还没建好就当没证据
            done = None
    wired = bool(getattr(core, 'agents', None) is not None or repo is not None)
    if mode == 'strict' and endpoint:
        reason = ('预算模式为 strict：DSH 只暴露 4 个 RPC、没有逐次 usage，严格计费下的子 agent '
                  '只能跑本机命令；要用 DSH 就跑 bounded_estimate，或接受用量记为未知。')
    elif done:
        # 有一次真实跑完的调用 → 这条链路接通了。verification 只是那一件的核验结论，
        # 单独暴露出来，不参与 ready 判定。
        evidence_refs = ['companion_agent_tasks:' + done['id']]
        if done['verification'] != 'passed':
            reason = ('链路已接通（最近一次调用成功跑完）；那一件的核验结论是 %s，'
                      '这是单次成果的结论，不影响能力状态。' % done['verification'])
    elif endpoint or wired:
        reason = ('七操作与台账都已接线，但还没有一次真实跑完的调用；'
                  '先派一件小事跑通，这条才会变成 ready。')
    else:
        reason = '本机没有可用的 DSH 运行时；子 agent 只能跑 node=work（本机命令）。'
    item = _capability(
        AGENT_CAPABILITY, 'agent', '起独立上下文的子 agent 并回收结果',
        'ready' if (done and not (mode == 'strict' and endpoint)) else 'needs_setup',
        reason=reason, evidence=evidence_refs, checked_at=checked,
        schema_ref='life/companion_agents.py#OPS', node=node)
    # agent 是唯一没有 GUIDE 的 kind（它的说明在 companion.AGENT_INSTRUCTIONS）。
    # 这里补上同一份「怎么用」，按 op 说清参数，免得她照 AGENT_INSTRUCTIONS 的
    # 字面写法去发顶层字段——DECISION 只认 7 个键，别的都会 additionalProperties 拒绝。
    item['what'] = ('kind=agent + op(create/send/inspect/wait/cancel/resume/read_artifact)，'
                    '参数一律放进 arguments 这个 JSON 字符串')
    item['how_ref'] = 'life/companion.py#AGENT_INSTRUCTIONS'
    item['ops'] = list(OP_NAMES)
    # agent 是动作 kind，必须回答「能派活吗」：只有 ready 才真的能派。
    # needs_setup 时说 False —— 它与「掉线」的区别由 status + reason 承载，不由这个布尔承载。
    item['dispatchable'] = item['status'] == 'ready'
    return item


def _capability_line(item):
    """主上下文只要摘要：成功的一行带过，说不通的必须把原因写出来。"""
    if item['status'] == 'ready':
        return '%s %s' % (item['capability_ref'], item['status'])
    line = '%s %s' % (item['capability_ref'], item['status'])
    if item.get('reason'):
        line += '（' + str(item['reason'])[:90] + '）'
    return line


# 每种动作「干什么」的一句话。摘要层用：只描述职责，不宣布可用、不含参数——
# 可用性由 status 说，参数在 GUIDES 与 memory op=capabilities 里。
# 这不是能力清单：有哪些 kind、能不能用，永远来自执行器与真实探测。
PURPOSE = {
    'command': '在云主机上跑命令',
    'work': '读写工作区文件、跑测试',
    'web': '上网搜索、读网页',
    'skill': '查和存自己的做法库',
    'memory': '回查自己做过的动作和回执',
    'growth': '记自己的认识与兴趣',
    'dsh': '把多步工作整体交给后端代理',
    'computer': '操作主人电脑桌面',
    'phone': '操作手机界面',
    'body': '桌面形象身体',
    'town': '去小镇和角色互动',
    'bilibili': 'B 站搜索、看视频',
}


def _prompt_line(item):
    """摘要里这一条写什么：能用的写一句它干什么，连接类写它的真实处境。

    guard/channel 的 ``what`` 是「现在怎样」（有人兜底吗、发得出消息吗），
    这个必须每轮看见；动作种类用固定的短句——它们没有随轮变化的状态。
    """
    ref = str(item.get('capability_ref') or '')
    if ref.startswith('channel.') or ref == 'guard.watchdog':
        return str(item.get('what') or _purpose(item))[:36]
    return _purpose(item)


def _purpose(item):
    """一句话说这个能力是干什么的（摘要层用，不含参数）。"""
    ref = str(item.get('capability_ref') or '')
    if ref.startswith('channel.'):
        return '消息通道'
    if ref == 'guard.watchdog':
        return '改坏源码时的自动回滚'
    if ref == AGENT_CAPABILITY:
        return '派子 agent 做完整一件事'
    return PURPOSE.get(ref.replace('action.', ''), '动作')


# 第一层索引里每一项的「处境」短词（3~8 字）。**这是给名字配的一句话，
# 不是完整原因**：完整原因在条目里，用 memory op=capabilities 读。
# 按 capability_ref 查，查不到就用状态本身当词——宁可短，不可把原因塞进来。
SHORT_STATE = {
    'guard.watchdog': {('offline',): '回滚守护/当前停用', ('ready',): '回滚守护/在岗',
                       ('unknown',): '回滚守护/状态不明'},
    'action.phone': {('offline',): '手机未连', ('unknown',): '手机未连', ('ready',): '手机在线'},
    'action.computer': {('offline',): '桌面未连', ('ready',): '桌面已连'},
    'action.body': {('offline',): '身体未连', ('ready',): '身体已连'},
    'action.town': {('needs_setup',): '小镇未启用', ('ready',): '小镇在线',
                    ('offline',): '小镇暂停', ('unknown',): '小镇状态不明'},
    'action.bilibili': {('needs_setup',): 'B站未登录', ('ready',): 'B站已登录'},
    'action.dsh': {('needs_setup',): '后端代理不可用'},
    'agent.subagent': {('needs_setup',): '子 agent 待跑通', ('ready',): '子 agent 可用'},
    'channel.qq': {('offline',): 'QQ 通道关闭', ('needs_setup',): 'QQ 未登录'},
    'channel.wechat': {('offline',): '微信通道关闭', ('needs_setup',): '微信未登录'},
}

# 状态本身的一句话兜底（比原因为短，且不抢位置）。
# unknown 写成「这次没报」而不是「状态不明」：执行器这一轮压根没把它报成可用，
# 那是「这轮没拿到」的意思，不是「它坏了」，更不是「它不存在」。
STATUS_WORD = {'offline': '未连接', 'needs_setup': '未配好', 'unknown': '这轮没报可用',
               'unsupported': '不支持', 'disabled': '已关闭'}


def _short_state(item):
    """第一层索引用：3~8 个字的处境词，绝不把完整原因写进主上下文。"""
    ref = str(item.get('capability_ref') or '')
    status = str(item.get('status') or 'unknown')
    return (SHORT_STATE.get(ref) or {}).get((status,)) or STATUS_WORD.get(status, '状态不明')


def capability_summary(catalogue):
    """主上下文里的能力摘要：三层分开写——能用什么、不能用的是什么原因、细节去哪查。

    - ``usable``：一句话一条，仅当轮执行器真的报成可用。
    - ``unusable``：能力名 + 具体原因。**掉线的不会从这里消失**，所以「手机怎么
      连不上了」有答案，而不是留一个沉默的缺口。
    - ``how``：要参数细节用哪个动作读，不把完整 schema 塞进每一轮。

    ``ready``/``not_ready`` 保留旧形状（下游有按这两个键读的地方），内容与上面一致。
    """
    catalogue = list(catalogue or [])
    gaps = [item for item in catalogue if item.get('status') != 'ready']
    counts = {}
    for item in catalogue:
        counts[item['status']] = counts.get(item['status'], 0) + 1
    # ===== 第一层：常驻索引（每轮都发，极小）=====
    # 设计口径（用户 2026-11-18）：「想找的时候信息都有，不用的时候不用占很大上下文」。
    # 所以**每一项都出现名字**（否则她不知道有这东西，也就不会去查），
    # 但每项只给 3~8 个字的处境，完整原因/参数/示例一律不进主上下文。
    usable = ['%s（%s）' % (str(item['capability_ref']).replace('action.', ''), _prompt_line(item))
              for item in catalogue if item.get('status') == 'ready']
    unusable = ['%s（%s）' % (str(item['capability_ref']).replace('action.', ''), _short_state(item))
                for item in gaps]
    return dict(total=len(catalogue),
                counts=counts,
                usable=usable,
                unusable=unusable,
                # 最后一句必须说清「怎么查」，否则名字给了她也用不上。
                how=('上面每项只有一句处境，不带参数。要用某一项、或想知道它为什么不能用，'
                     '就用 memory 动作 op=capabilities（可加 arguments{"status":"offline"} 过滤）'
                     '读整条：完整原因、参数要点、检查时刻都在那。'),
                ready=len(usable),
                not_ready=len(gaps),
                note='名字都在这里了，没有一个被省略；不在「可用」里只是此刻用不了，'
                     '不是不存在，完整原因用 memory op=capabilities 读。')


def _agent_tasks(core, observation, now):
    """活动任务与新结果：只认持久台账里的真实状态，不认模型的乐观说法。"""
    agent_id = getattr(getattr(core, 'life', None), 'agent_id', None)
    if not agent_id:
        return []
    rows = core.repo._all(
        "SELECT id,project_ref,action,status,updated FROM companion_actions "
        "WHERE agent=? AND status IN ('pending','running') ORDER BY updated DESC LIMIT 5", (agent_id,))
    project = (observation or {}).get('project') or (observation or {}).get('settled_project_ref')
    rows = list(rows)
    if project:
        rows = sorted(rows, key=lambda row: 0 if row['project_ref'] == project else 1)
    seen = set()
    out = []
    for row in rows:
        if row['project_ref'] in seen:
            continue
        seen.add(row['project_ref'])
        action = json.loads(row['action'] or '{}')
        count = core.repo._one(
            "SELECT COUNT(*) AS n FROM companion_activity WHERE agent=? AND type='action_result' "
            "AND json_extract(details,'$.project_ref')=? AND created>?",
            (agent_id, row['project_ref'], float(row['updated'] or 0))) or {}
        out.append(dict(task_ref=row['id'], project_ref=row['project_ref'],
                        state=row['status'], node=NODE,
                        op=action.get('kind'),
                        goal=str(action.get('goal') or action.get('command') or '')[:120],
                        last_progress_at=_stamp(row['updated']),
                        # repo._one 返回 sqlite3.Row：只能按列名取值，不能 .get()。
                        new_results=int(count['n']) if count and count['n'] is not None else 0,
                        stale=row['status'] == 'running',
                        source_ref='companion_actions:' + row['id'],
                        observed_at=_stamp(now)))
    waked = (observation or {}).get('settled_project_ref')
    if waked and not any(row['project_ref'] == waked for row in out):
        latest = core.repo._all(
            "SELECT id,status,updated FROM companion_actions WHERE agent=? AND project_ref=? "
            "ORDER BY updated DESC LIMIT 1", (agent_id, waked))
        if latest:
            row = latest[0]
            out.append(dict(task_ref=row['id'], project_ref=waked,
                            state=row['status'], node=NODE, op=None, goal='',
                            last_progress_at=_stamp(row['updated']),
                            new_results=1, stale=False,
                            source_ref='companion_actions:' + row['id'],
                            observed_at=_stamp(now)))
    return out


def _errors(state):
    errors = list(state.get('projection_errors') or [])
    current = (state.get('current_state') or {}).get('projection_errors') or []
    errors.extend(dict(section='current_state.' + str(item).split(':', 1)[0],
                       error=str(item).split(':', 1)[-1], detail='')
                  for item in current)
    return errors


def build(core, *, state=None, current=None, wake_kind=None, attention=None, now=None):
    """返回只读投影。字段为 None / 空表示未知，调用方不能自行补造。

    ``state`` 是本轮已经组好的上下文投影（``_messages`` 里那个 dict）：只读它，
    不改它。缺省时各段退化为「未知」而不是编造。
    """
    now = time.time() if now is None else float(now)
    state = dict(state or {})
    relaxed = state.pop('_awareness_relaxed', 0) or 0
    errors = []

    def run(name, fn, default=None):
        if name in ('recent_experience', 'agent_tasks') and relaxed:
            # 请求超预算时的降级顺序：先压这两段，不压账号与能力事实。
            return default
        return _section(name, errors, fn, default)

    catalogue = run('capabilities', lambda: capabilities(core, state, now=now), [])
    tasks = run('agent_tasks', lambda: _agent_tasks(core, None, now), [])
    projection = dict(
        observed_at=_stamp(now),
        projection_version=PROJECTION_VERSION,
        schema=SCHEMA_VERSION,
        node=NODE,
        self=dict(
            agent=run('self.agent', lambda: _self_agent(core)),
            account=run('self.account', lambda: _self_account(core, core.cfg)),
        ),
        runtime=run('runtime', lambda: _runtime(core, state)),
        here=run('here', lambda: _here(core, current, wake_kind)),
        attention=run('attention', lambda: _attention(core, attention, now), []),
        recent_experience=run('recent_experience',
                              lambda: _recent_experience(core, current, now), []),
        # 主上下文只放摘要：全表用 memory op=capabilities 按需读。
        capabilities=run('capabilities.summary', lambda: capability_summary(catalogue),
                         capability_summary([])),
        agent_tasks=run('agent_tasks.summary', lambda: _compact_tasks(tasks), []),
        # 当前关注之外还要知道「这一次醒来为什么、下一次什么时候」。
        wake=dict(kind=wake_kind or ('user' if current is not None else 'timer'),
                  next_attention_at=_stamp(core._state('next_wake')),
                  source_ref='meta:companion_next_wake', observed_at=_stamp(now)),
        projection_errors=_errors(dict(projection_errors=errors,
                                       current_state=state.get('current_state'))),
    )
    projection['_catalogue'] = catalogue
    projection['_tasks'] = tasks
    if relaxed:
        projection['degraded'] = True
    return projection


def public_view(projection):
    """请求体用的视图：去掉内部索引（_catalogue/_tasks），只留可读的投影。"""
    return {key: value for key, value in (projection or {}).items() if not key.startswith('_')}


def _compact_tasks(tasks):
    """摘要：只留判断「还有几件、哪件有新结果」需要的字段。"""
    out = []
    for row in tasks or []:
        out.append(dict(task_ref=row.get('task_ref'), state=row.get('state'),
                        goal=(row.get('goal') or row.get('op') or '')[:60],
                        new_results=row.get('new_results'), last_progress_at=row.get('last_progress_at')))
    return out


def full_catalogue(projection):
    """按需读取用：拿回完整能力目录（含 evidence/schema_ref）。"""
    return list((projection or {}).get('_catalogue') or [])


def full_tasks(projection):
    return list((projection or {}).get('_tasks') or [])


def observation_tasks(core, observation, *, now=None):
    """结果轮专用补充：把刚结算的那件事排到最前，便于「本轮有什么新结果」。"""
    now = time.time() if now is None else float(now)
    errors = []
    return _section('agent_tasks.observation', errors,
                    lambda: _agent_tasks(core, observation, now), [])


# ------------------------------------------------------------------ 按需读取

def _page(items, fields, default_limit=8, max_limit=20):
    """分页与截断说明：next_cursor 与 truncation 都要能被人直接读懂。"""
    try:
        limit = int(fields.get('limit') or default_limit)
    except (TypeError, ValueError):
        limit = default_limit
    limit = max(1, min(limit, max_limit))
    cursor = str(fields.get('cursor') or '')
    start = 0
    if cursor.startswith('awareness:'):
        try:
            start = max(0, int(cursor.split(':', 1)[1]))
        except ValueError:
            start = 0
    window = items[start:start + limit]
    remaining = len(items) - start - len(window)
    more = remaining > 0
    return dict(items=window, next_cursor=('awareness:%d' % (start + len(window))) if more else None,
                returned=len(window), available=len(items),
                truncation=(dict(truncated=True, reason='还有 %d 条未返回，用 next_cursor 继续' % remaining)
                            if more else dict(truncated=False, reason='')))


def _freshness(projection):
    return dict(observed_at=projection.get('observed_at'),
                observed_at_ts=time.time(),
                node=projection.get('node'),
                basis='assembly_time',
                meaning='这是组装这次读取的时刻；每条记录的自己的 observed_at 见条目内。')


def _reply(op, projection, page, source_refs, *, status='completed', note=''):
    return dict(status=status, op=op, items=page['items'], next_cursor=page['next_cursor'],
                source_refs=list(dict.fromkeys(source_refs)), freshness=_freshness(projection),
                truncation=page['truncation'], returned=page['returned'],
                available=page['available'], schema=SCHEMA_VERSION,
                evidence_scope='projection',
                evidence_note=note or '这是运行时投影，不是模型描述；未知即 null，别补造。',
                output=_summary(op, page, projection))


def _used(core, fields, projection):
    """「这些能力我用过没有、上次怎么样」——只从动作台账提炼，只读。

    与 ``capabilities`` 的分工：那边答「现在能不能用」，这边答「我用过没有」。
    两者都不能替对方回答，所以两条都列出来，别拿一条当另一条。
    """
    repo = getattr(core, 'repo', None)
    agent_id = getattr(getattr(core, 'life', None), 'agent_id', None)
    if repo is None or not agent_id:
        return dict(status='failed', op='used', items=[], next_cursor=None, source_refs=[],
                    freshness=_freshness(projection), truncation=dict(truncated=False, reason=''),
                    returned=0, available=0, schema=SCHEMA_VERSION,
                    reason='没有接上台账，查不到自己的经历',
                    evidence_scope='none',
                    output='这次读不到动作台账，所以「用过没有」查不了；不能说成「没做过」。')
    kinds = fields.get('kind') or fields.get('kinds') or ''
    if isinstance(kinds, str):
        kinds = [part for part in kinds.replace('，', ',').split(',') if part.strip()]
    kinds = [str(item).strip() for item in kinds if str(item).strip()]
    since, until = _window(fields)
    if _WINDOW_UNPARSED in (since, until):
        sample = fields.get('since') if since == _WINDOW_UNPARSED else fields.get('until')
        return dict(status='failed', op='used', items=[], next_cursor=None, source_refs=[],
                    freshness=_freshness(projection), truncation=dict(truncated=False, reason=''),
                    returned=0, available=0, schema=SCHEMA_VERSION,
                    reason='时间窗看不懂：%r' % (sample,),
                    evidence_scope='none',
                    output='时间窗只能是秒级时间戳，或 %s 这样的写法；没有按你给的条件去查，'
                           '所以这次不是「查无此事」。' % '2026-11-17 09:00')
    from .companion_experience import Experience
    report = Experience(repo, agent_id).by_kind(kinds=kinds, since=since, until=until)
    items = report['kinds']
    page = _page(items, fields)
    return dict(status='completed', op='used', items=page['items'], next_cursor=page['next_cursor'],
                source_refs=['companion_actions'] + (['companion_agent_tasks'] if report['reported'] else []),
                freshness=_freshness(projection), truncation=page['truncation'],
                returned=page['returned'], available=page['available'], schema=SCHEMA_VERSION,
                window=report['window'], reported=report['reported'],
                basis_kinds=sorted({item['basis'] for item in items}),
                evidence_scope='action_ledger',
                evidence_note=report['scope'],
                output=_used_summary(page, report))


def _used_summary(page, report):
    """主回执的一句话：分得开「用过」与「没做过」，也不把一次失败说成永久结论。"""
    items = page['items']
    if not items:
        return ('这段时间里台账没有任何动作。没做过不等于做不到；'
                '现在的可用状态看 capabilities。')
    used = [item for item in items if item.get('counts', {}).get('total')]
    never = [item for item in items if item['standing']['state'] == 'never_used']
    lines = []
    for item in used:
        counts = item['counts']
        last = item.get('last') or {}
        verb = '成功' if last.get('outcome') == 'ok' else '失败'
        basis = '亲历' if item['basis'] == 'first_hand' else '听它自己说（非亲历）'
        lines.append('%s 用过 %d 次（成功 %d/失败 %d）；最近一次 %s %s，id=%s，%s'
                     % (item['kind'], counts['total'], counts['ok'], counts['failed'],
                        last.get('at') or '时间未知', verb, last.get('action_id'),
                        basis))
    # 句式必须跟着事实走：没有一项用过时，不能说「用过 0 项：」——
    # 那是把「没做过」说成了「用过」（t5 的 F-5，repro-used-never-empty.py 实测）。
    if used:
        text = '用过 %d 项：%s' % (len(used), '；'.join(lines))
    else:
        text = '这段时间里一项都没用过，没有任何真实回执。'
    if never:
        text += ('没做过的是：' + '、'.join(item['kind'] for item in never)
                 + '（只是没做过，不等于做不到）')
    if report['reported']:
        text += '。另有 %d 条是子 agent 台账里它自己的说法，不是亲历' % len(report['reported'])
    return text


_WINDOW_UNPARSED = object()


def _window(fields):
    """时间窗解析：接受秒级时间戳，也接受本地日期/时间字符串。

    看不懂就返回 ``_WINDOW_UNPARSED``，由调用方明说「这次没按条件查」——
    静默忽略过滤器会变成「拿全量冒充筛选结果」。
    """
    out = []
    for key in ('since', 'until'):
        value = fields.get(key)
        if value in (None, ''):
            out.append(None)
            continue
        parsed = _parse_window(value)
        out.append(_WINDOW_UNPARSED if parsed is None else parsed)
    return out[0], out[1]


def _parse_window(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    for pattern in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
        try:
            return datetime.strptime(text, pattern).timestamp()
        except ValueError:
            continue
    return None


def _summary(op, page, projection):
    items = page['items']
    if op == 'capabilities':
        counts = {}
        for item in items:
            counts[item.get('status')] = counts.get(item.get('status'), 0) + 1
        gaps = [item for item in items if item.get('status') != 'ready']
        text = '能力 %d 条：%s' % (len(items), '，'.join('%s=%d' % kv for kv in sorted(counts.items())))
        if gaps:
            text += '。不能用的：' + '；'.join('%s（%s）' % (item.get('capability_ref'), item.get('reason') or '没给原因')
                                             for item in gaps[:4])
        return text
    if op == 'attention':
        if not items:
            return '现在没有单独设过关注；本轮按正常对话处理。'
        return '关注 %d 条：%s' % (len(items), '；'.join(
            '%s=%s%s' % (item.get('conversation_ref'), item.get('mode'),
                         ('（%s）' % item['reason']) if item.get('reason') else '') for item in items[:4]))
    if op == 'agent_tasks':
        if not items:
            return '手上没有在跑或排队的事。'
        return '在办 %d 件：%s' % (len(items), '；'.join(
            '%s｜%s｜新结果 %s 条' % (item.get('goal') or item.get('op') or item.get('task_ref'),
                                    item.get('state'), item.get('new_results')) for item in items[:4]))
    if op == 'awareness':
        account = ((projection.get('self') or {}).get('account') or {})
        caps = projection.get('capabilities') or {}
        return ('会话 %s；账号 %s；可用动作 %s 种；关注 %d 条；在办 %d 件；能力 %s（ready %s）' % (
            ((projection.get('here') or {}).get('conversation_ref') or '未知'),
            account.get('state'),
            len((projection.get('runtime') or {}).get('available_actions') or []),
            len(projection.get('attention') or []),
            len(projection.get('agent_tasks') or []),
            caps.get('total'), len(caps.get('usable') or [])))
    return '返回 %d 条。' % len(items)


def _not_implemented(op, projection, reason):
    return dict(status='not_implemented', op=op, items=[], next_cursor=None,
                source_refs=['docs/companion-implementation-plan.md#3.4'],
                freshness=_freshness(projection),
                truncation=dict(truncated=False, reason='这一层还没实现，没有可返回的数据'),
                returned=0, available=None, reason=reason, schema=SCHEMA_VERSION,
                evidence_scope='not_implemented',
                evidence_note='这里空着不代表「查无此事」，只代表这一类读取还没接上。',
                output='暂时查不了：' + reason)


def read(core, action, *, state=None, current=None, wake_kind=None, attention=None,
         now=None, projection=None):
    """按需读取的统一入口：memory 动作的扩展 op，不新增 kind。

    ``op in {awareness, capabilities, attention, agent_tasks}`` 返回运行时投影；
    ``people/conversations/experience`` 显式 not_implemented 并给原因——空数组
    会被读成「查无此事」，那是在说谎。
    """
    fields = _fields(action)
    op = str(fields.get('op') or 'awareness')
    if not isinstance(projection, dict):
        projection = build(core, state=state, current=current, wake_kind=wake_kind,
                           attention=attention, now=now)
    if op in NOT_IMPLEMENTED:
        return _not_implemented(op, projection, NOT_IMPLEMENTED[op])
    if op == 'awareness':
        # 投影里的 _catalogue/_tasks 是内部索引，不进请求体也不进回执。
        page = _page([public_view(projection)], fields, default_limit=1, max_limit=1)
        return _reply(op, projection, page, ['life/companion_awareness.py#build'],
                      note='这是整套投影的摘要快照；要某一类细节就单独读那一类。')
    if op == 'capabilities':
        items = full_catalogue(projection)
        status = str(fields.get('status') or '')
        if status:
            items = [item for item in items if item.get('status') == status]
        page = _page(items, fields)
        return _reply(op, projection, page, ['life/companion_awareness.py#capabilities'])
    if op == 'attention':
        items = list(projection.get('attention') or [])
        conv = fields.get('conversation_ref')
        if conv:
            items = [item for item in items if item.get('conversation_ref') == conv]
        page = _page(items, fields)
        return _reply(op, projection, page, ['meta:companion_attention'])
    if op == 'agent_tasks':
        items = full_tasks(projection)
        ref = fields.get('ref')
        if ref:
            items = [item for item in items if ref in (item.get('task_ref'), item.get('project_ref'))]
        page = _page(items, fields)
        return _reply(op, projection, page, ['companion_actions'])
    if op == 'used':
        return _used(core, fields, projection)
    return dict(status='failed', op=op, items=[], next_cursor=None, source_refs=[],
                freshness=_freshness(projection),
                truncation=dict(truncated=False, reason=''),
                reason='memory 的读 op 只支持 %s' % '/'.join(READ_OPS),
                output='不认识的读取类型：%s；可用的是 %s' % (op, '/'.join(READ_OPS)))


def current_projection(core, *, now=None, reuse=True):
    """这一轮实际装进请求体的那段投影；没有就现场组一份（失败退回现场组）。

    ``reuse`` 的目的是「读到你看到的同一份事实」：工具轮和主轮之间若重新拼，
    会出现两套 observed_at 和两份账号状态，读出来的东西跟眼前对不上。
    """
    if reuse:
        sticky = getattr(core, '_last_awareness', None)
        if isinstance(sticky, dict) and sticky.get('projection_version') == PROJECTION_VERSION:
            return sticky
    state = getattr(core, '_last_awareness_context', None) or {}
    envelope = getattr(core, '_last_awareness_wake_kind', None) or {}
    return build(core, state=state if isinstance(state, dict) else {},
                 current=getattr(core, '_last_awareness_current', None),
                 wake_kind=(envelope.get('kind') if isinstance(envelope, dict) else None)
                     or ('user' if getattr(core, '_last_awareness_current', None) is not None else 'timer'),
                 now=now)


def set_attention(core, action, *, now=None):
    """关注变更：写 meta 里的关注表（带 since/review_at/expires_at），不改别的。"""
    now = time.time() if now is None else float(now)
    fields = _fields(action)
    mode = str(fields.get('mode') or '').strip()
    if mode not in ATTENTION_MODES:
        return dict(status='failed', op='attention_set', items=[], next_cursor=None,
                    reason='mode 只能是 %s' % '/'.join(ATTENTION_MODES),
                    output='关注模式无效：%r' % mode)
    conv = str(fields.get('conversation_ref') or '').strip()
    if not conv:
        return dict(status='failed', op='attention_set', items=[], next_cursor=None,
                    reason='attention_set 需要 conversation_ref',
                    output='要改关注哪一段会话，就得说清 conversation_ref。')
    rows = list(core._state('companion_attention') or [])
    expires = fields.get('expires_at')
    review = fields.get('review_at')
    row = dict(conversation_ref=conv, mode=mode, reason=str(fields.get('reason') or '')[:240],
               since=now, review_at=_parse(review) if not isinstance(review, str) else review,
               expires_at=_parse(expires) if not isinstance(expires, str) else expires,
               cursor=fields.get('cursor') or None, updated=now)
    replaced = False
    for index, item in enumerate(rows):
        if isinstance(item, dict) and item.get('conversation_ref') == conv:
            rows[index] = row
            replaced = True
            break
    if not replaced:
        rows.append(row)
    with core.repo.tx():
        core._save('companion_attention', rows[-32:])
    shown = dict(row, since=_stamp(row['since']), review_at=_stamp(row.get('review_at')),
                 expires_at=_stamp(row.get('expires_at')))
    return dict(status='completed', op='attention_set', items=[shown], next_cursor=None,
                source_refs=['meta:companion_attention'], freshness=_freshness({
                    'observed_at': _stamp(now), 'node': NODE}),
                truncation=dict(truncated=False, reason=''),
                evidence_scope='stored', evidence_note='关注已保存；这是设置回执，不是别人已读的证据。',
                output='%s 现在的关注是 %s。' % (conv, mode))


async def run(core, action, **context):
    """memory 动作里扩展出来的读/设 op。

    ``_fields`` 同时认顶层 op 和 arguments 里的 op；两种写法都已经被这里认领了，
    兜底时不能再把它们按 search 转给回查——那样会答非所问。
    """
    fields = _fields(action)
    op = str(fields.get('op') or 'search')
    if op == 'attention_set':
        return set_attention(core, action, now=context.get('now'))
    if op in READ_OPS:
        # 读的是这一轮已经装进请求体的事实，而不是重新拼一份。
        projection = current_projection(core, now=context.get('now'))
        state = context.get('state') if isinstance(context.get('state'), dict) else {}
        return read(core, action, state=state, current=context.get('current'),
                    wake_kind=context.get('wake_kind'), now=context.get('now'),
                    projection=projection)
    return dict(status='failed', op=op, items=[], next_cursor=None, source_refs=[],
                reason='不认识的 memory op：%s' % op,
                output='memory 的 op 只支持 search/read 以及 %s；收到的是 %s'
                       % ('/'.join(READ_OPS + SET_OPS), op))


# ------------------------------------------------------------------ 提示词

SUMMARY = (
    'awareness 是运行时投影，不是记忆，也不是你说了算的：\n'
    '- self.account 是登录核验过的账号事实：value 为 null 就是还不知道，state 可能是 '
    'connected/disconnected/disabled/unconfigured/login_required/unknown。disabled 表示通道被关了，'
    '不能当成「已连上」；unknown 就明说不知道，不要猜别人的号。\n'
    '- here.actor_ref 是这轮说话的人，account_ref 是你用的号，conversation_ref 是这段会话；'
    '三者是三个东西，不要混着说。\n'
    '- attention 是带期限的关注（focused/watching/muted/released），released 表示已经放下；'
    'expires_at 到了就自动放下，不要还当在关注。\n'
    '- agent_tasks 是真实台账里排队或正在跑的事，new_results 是这件之后到的新结果条数；'
    'queued/running 都不是完成。\n'
    '- capabilities 是**索引**不是说明书：usable 与 unusable 都把每个能力的名字列全了，'
    '后面括号里只有 3~8 个字的处境（例如「手机（手机未连）」）。名字在，说明它存在；'
    '不在「可用」里只是此刻用不了。**完整原因、参数要点、检查时刻都不在这里**，'
    '要就按 how 那句话说的方法读；不要把 idx 里的一句话当成完整原因复述，也不要因为'
    '某条只写了处境就说成「没有这个能力」。状态词是 ready/offline/needs_setup/unknown，'
    'unknown 一律说「不知道」，别说成 offline 或没有。\n'
    '- 要细节用 memory 动作：op=awareness/capabilities/attention/agent_tasks 读投影，'
    'op=attention_set 加 conversation_ref/mode/reason/review_at/expires_at 改关注。'
    'people/conversations/experience 现在返回 not_implemented——那表示「这一层还没接上」，'
    '不表示「查无此事」。\n'
    '结果由程序按真实回执记录；你写的成功不算成功。'
)
GUIDE = (
    'awareness 是这一轮的运行事实（只读投影），不是记忆：self.account 是登录核验过的账号，'
    'value 为 null 就是不知道，disabled 是被关了、不能当成在线；here 里的 actor_ref（说话的人）、'
    'account_ref（你用的号）、conversation_ref（这段会话）是三样东西，别混着说。'
    'attention 里 released 或已到期的关注已经放下，别再当在关注；agent_tasks 里 queued/running 都还没完成。'
    'capabilities 是索引：每个能力的名字都在，括号里只有几个字的处境；完整原因与参数用 '
    'memory op=capabilities 读整条。写着处境不等于没有这个能力，别把它说成不存在。'
    '要细节用 memory 动作：op=awareness/capabilities/attention/agent_tasks 按需读，'
    'op=attention_set 加 conversation_ref/mode/reason/review_at/expires_at 改关注；'
    'people/conversations/experience 返回 not_implemented，意思是这一层还没接上，不是「查无此事」。'
)
