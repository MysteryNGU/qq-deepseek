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
READ_OPS = ('awareness', 'people', 'conversations', 'experience',
            'attention', 'capabilities', 'agent_tasks')
SET_OPS = ('attention_set',)
NOT_IMPLEMENTED = {
    'people': '按需读取人物档案需要 C 阶段的人物模型（跨会话、跨平台合并同一人、'
              '非主人的可见边界）。本阶段只投影当前这段会话里的人，不做跨会话检索。',
    'conversations': '会话索引进 C 阶段（QQ 多人交流与 conversation_ref 路由同时落地）。'
                     '现在只有当前这段会话的 conversation_ref，没有全量会话表可列。',
    'experience': '经历时间线要按人分层的可见性策略，属 C 阶段。'
                  '本阶段只用 recent_experience 给当前会话里的几条，不做检索。',
}

BEIJING = timezone(timedelta(hours=8))


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
                'review_at', 'expires_at', 'goal'):
        if action.get(key) not in (None, '') and merged.get(key) in (None, ''):
            merged[key] = action[key]
    return merged


def spending_mode(policy):
    """兼容旧调用：真正的模式判定在 ``companion_agents.budget_mode``（含真实配置对象）。"""
    from .companion_agents import budget_mode
    return budget_mode(policy)


def _legacy_spending_mode(policy):
    """预算模式：off | strict | bounded_estimate。

    P0 实测 DSH 只暴露 4 个 RPC、无 usage 字段，故 strict 对 DSH 结构性不可行。
    既有配置只有 enabled 布尔值：True 视为用户已选的 strict（**不静默改成
    bounded_estimate**，设计 §6.3），显式配置里写了 mode 就按配置。
    """
    if policy is None:
        return 'off'
    if not getattr(policy, 'enabled', False):
        return 'off'
    mode = getattr(policy, 'mode', None)
    if mode in {'off', 'strict', 'bounded_estimate'}:
        return mode
    return 'strict'


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


def _self_account(core, config):
    """账号事实只来自登录核验，不来自 persona，也不来自 cfg.bot_qq 的存在与否。

    三处来源，从新到旧：ImManager/CompanionQQ 的 snapshot、进程持有的 qq 对象、
    通道配置文件。任何一处都拿不到就是 unknown；通道没启用是 disabled。
    """
    observed = time.time()
    try:
        from .companion_qq import load_qq_settings
        settings = load_qq_settings(core.home) or {}
    except Exception:
        settings = {}
    source_ref = 'life/companion_qq.py#load_qq_settings'
    enabled = bool(settings.get('enabled'))
    snapshot = None
    state = None
    error = ''
    server = _server_of(core)
    im = getattr(server, 'im', None) if server is not None else None
    if im is None and server is not None:
        # 服务先起、IM 后起的那一小段：直接读通道管理器，问它现在的状态。
        factory = getattr(server, 'im_factory', None)
        if callable(factory):
            try:
                im = factory()
            except Exception as failure:
                error = type(failure).__name__
    if im is not None:
        try:
            snapshot = (im.snapshot() or {}).get('qq')
            source_ref = 'life/companion_im.py#ImManager.snapshot'
        except Exception as failure:
            error = type(failure).__name__
    qq = getattr(im, 'qq', None) if im is not None else None
    if snapshot is None and qq is not None:
        try:
            snapshot = qq.snapshot()
            source_ref = 'life/companion_qq.py#CompanionQQ.snapshot'
        except Exception as failure:
            error = error or type(failure).__name__
    if snapshot is not None:
        state = str(snapshot.get('state') or 'unknown')
        error = error or str(snapshot.get('error') or '')
        # account 是登录核验过才有的值；核对前不把配置里的号当成在用。
        value = snapshot.get('account') if state == 'connected' else None
        value = str(value) if value not in (None, '', 0, '0') else None
    else:
        value = None
        if not enabled:
            state = 'disabled'
        elif settings.get('login_required'):
            state = 'login_required'
        elif settings:
            state = 'unknown'
        else:
            state = 'unconfigured'
    if state not in ACCOUNT_STATES:
        state = 'unknown'
    return dict(value=value, value_visibility='owner-only', state=state, enabled=enabled,
                error=(error or None), source_ref=source_ref,
                source_scope='runtime_snapshot' if snapshot is not None else 'channel_config',
                observed_at=_stamp(observed))


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
    """沿执行器链找到真正持有 DSH 配置的那一层；拿不到就是未知（None）。"""
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


def capabilities(core, state=None, *, now=None):
    """能力目录：每条都能落到「谁在用、凭什么说它能用」。

    ready 只给有真实成功调用证据的；动作种类只证明通道在此节点存在，不证明
    这次跑成功过。agent 能力在计划 B 接线前一律 needs_setup。
    """
    now = time.time() if now is None else float(now)
    state = state or {}
    checked = _stamp(now)
    catalogue = []
    # 键 'agent' 也是动作种类（kind=agent），但它的能力条目是下面的
    # agent.subagent，不能再按「动作种类」重复登记成一条 ready。
    kinds = [kind for kind in (state.get('available_actions') or []) if kind != 'agent']
    # dsh 的条目由下面对应的那条统一登记（带 strict 原因），这里先跳过它，
    # 否则会多出一条「动作种类 ready」和一条「strict 下 needs_setup」自相矛盾。
    for kind in [kind for kind in kinds if kind != 'dsh']:
        catalogue.append(_capability(
            'action.' + str(kind), 'action', '动作种类 ' + str(kind),
            'ready', evidence=['available_actions'], checked_at=checked,
            schema_ref='life/companion.py#DECISION.actions'))
    mode = spending_mode(getattr(getattr(core, 'spending', None), 'policy', None))
    # DSH 是否存在：看真正跑 dsh 的执行器链（服务模式下是 RemoteExecutor.original）。
    executor = getattr(core, 'executor', None)
    node = NODE
    endpoint = _dsh_endpoint(executor)
    dsh_reason = None
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
    catalogue.append(_capability(
        'action.dsh', 'action', '把一整件多步工作交给后端代理', dsh_status,
        reason=dsh_reason, evidence=['cfg.dsh_root'] if endpoint else [],
        checked_at=checked, schema_ref='life/companion.py#kind:dsh', node=node))
    # 子 agent 能力：计划 B 已接线（life/companion_agents.py）。ready 只在台账里真有过
    # 一次「跑完并核验通过」时才给；否则写 needs_setup 并说清缺的是哪一步。
    catalogue.append(agent_capability(core, mode=mode, endpoint=endpoint, now=now))
    # 通道是否启用看执行器有没有报可用动作；上面已经按种类筛过，这里不能复用
    # 那个（可能为空的）列表，否则「没报动作」会被读成「通道没开」。
    actions_reported = state.get('available_actions') is not None
    for channel in ('qq', 'wechat'):
        catalogue.append(_capability(
            'channel.' + channel, 'channel', '消息通道：' + channel,
            'ready' if actions_reported else 'unknown',
            reason=None if actions_reported else '执行器没有报告可用动作，无法判断通道状态',
            checked_at=checked, schema_ref='life/companion_im.py#ImManager'))
    return catalogue[:24]


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
    proven = None
    if repo is not None:
        try:
            row = repo._one("SELECT id FROM companion_agent_tasks WHERE agent=? AND status='completed' "
                            "AND verification='passed' ORDER BY updated DESC LIMIT 1",
                            (getattr(getattr(core, 'life', None), 'agent_id', None),))
            proven = row['id'] if row else None
        except Exception:  # noqa: BLE001 - 台账还没建好就当没证据
            proven = None
    if mode == 'strict' and endpoint:
        reason = ('预算模式为 strict：DSH 只暴露 4 个 RPC、没有逐次 usage，严格计费下的子 agent '
                  '只能跑本机命令；要用 DSH 就跑 bounded_estimate，或接受用量记为未知。')
    elif proven:
        evidence_refs = ['companion_agent_tasks:' + proven]
    elif endpoint:
        reason = ('七操作与台账都已接线，但还没有一次「完成并通过核验」的记录；'
                  '先派一件 node=work 的小事跑通，这条才会变成 ready。')
    else:
        reason = '本机没有可用的 DSH 运行时；子 agent 只能跑 node=work（本机命令）。'
    return _capability(
        AGENT_CAPABILITY, 'agent', '起独立上下文的子 agent 并回收结果',
        'ready' if (proven and not (mode == 'strict' and endpoint)) else 'needs_setup',
        reason=reason, evidence=evidence_refs, checked_at=checked,
        schema_ref='life/companion_agents.py#OPS', node=node)


def _capability_line(item):
    """主上下文只要摘要：成功的一行带过，说不通的必须把原因写出来。"""
    if item['status'] == 'ready':
        return '%s %s' % (item['capability_ref'], item['status'])
    line = '%s %s' % (item['capability_ref'], item['status'])
    if item.get('reason'):
        line += '（' + str(item['reason'])[:90] + '）'
    return line


def capability_summary(catalogue):
    """主上下文里的能力摘要：一共几条、哪些不能用、为什么。"""
    catalogue = list(catalogue or [])
    gaps = [item for item in catalogue if item.get('status') != 'ready']
    counts = {}
    for item in catalogue:
        counts[item['status']] = counts.get(item['status'], 0) + 1
    return dict(total=len(catalogue),
                counts=counts,
                ready=[item['capability_ref'] for item in catalogue if item.get('status') == 'ready'],
                not_ready=[_capability_line(item) for item in gaps][:6],
                note='这里是摘要；细节用 memory op=capabilities 读，每条都有自己的 checked_at 与原因。')


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
                        new_results=int(count.get('n') or 0),
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
            caps.get('total'), len(caps.get('ready') or [])))
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
    '- capabilities 是摘要：total/counts/ready 列出能用的，not_ready 逐条给「为什么不能用」；'
    'status=ready 才真能用，needs_setup 就按原因说明缺什么，unknown 是还没探测、不等于没有。\n'
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
    'capabilities 只给摘要，ready 才真能用，not_ready 每条都写了原因，按原因说缺什么，不知道就说不知道。'
    '要细节用 memory 动作：op=awareness/capabilities/attention/agent_tasks 按需读，'
    'op=attention_set 加 conversation_ref/mode/reason/review_at/expires_at 改关注；'
    'people/conversations/experience 返回 not_implemented，意思是这一层还没接上，不是「查无此事」。'
)
