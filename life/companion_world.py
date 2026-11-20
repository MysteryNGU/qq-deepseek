"""World 事实层：把「世界现在是什么样」归一成一种形状。

**为什么要有这一层**

2026-11-18 审计（`docs/world-state-audit.md`）发现 15 个入口各说各话：
同一个布尔有三个键名（`connected` / `available` / `state`），三套状态词表，
只有 3/15 带齐 `source_ref` + `observed_at`。结果是读的人（她、前端、程序）
必须逐个适配，而「状态到底从哪来」谁也说不清。

这一层**不替换**任何现有投影。它只是把它们的返回值读出来、翻译成同一种形状，
让上层（认知投影、运行详情）有一个统一的地方可以问「现在世界是什么样」。

**三条硬规矩**

1. **不发明第四套词表。** `state` 只用 `capabilities` 那套 `STATES`
   （`ready/needs_setup/offline/unsupported/unknown`），因为它已经表达了
   最关键的那个区分：`offline` 是「探测到了、确实不可用」，`unknown` 是「读不到、不敢断言」。
2. **读不到就说 unknown，不猜。** 探测抛错、字段缺失、返回值形状不对，
   一律走 `unknown()`，detail 写清「读不到什么」，**不许写成断言**。
3. **只读。** 本模块不写库、不改任何被读取对象的返回值、不发网络请求。

**这里不含「进主上下文」的东西。** `raw` 是给按需读取和程序用的；
主上下文只该拿 `state` + `detail`（见 `brief()`）。
"""
from __future__ import annotations

import time

# 唯一的词表来源：与 life/companion_awareness.py 的 STATES 保持一致。
# 这里复制一份而不是 import，是为了让本模块不反向依赖 awareness
# （awareness 将来会反过来调用本模块）。
STATES = ('ready', 'needs_setup', 'offline', 'unsupported', 'unknown')

# 各类主体在「多久算旧」上的默认口径（秒）。仅用于 age_s 的计算与展示，
# **不参与任何判断**——阈值判定留给调用方。
DEFAULT_MAX_AGE = None


def _stamp(value):
    """ISO8601 秒级带 +08:00；拿不到就是 None，不编造。"""
    from datetime import datetime, timedelta, timezone
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return datetime.fromtimestamp(float(value), timezone(timedelta(hours=8))).isoformat(
        timespec='seconds')


def fact(fact_ref, subject, state, *, source_ref, observed_at=None, age_s=None,
         detail='', raw=None, **extra):
    """一条 world 事实。形状固定，额外字段走 **extra。

    ``fact_ref`` 是稳定标识（`phone.link`），不是描述——同一个主体在不同入口
    读到的必须是同一个 `fact_ref`，否则又回到各说各话。
    """
    if state not in STATES:
        raise ValueError('unknown world fact state: %r（只许用 %s）' % (state, STATES))
    if not fact_ref or not str(subject or '').strip():
        raise ValueError('fact 需要 fact_ref 与 subject')
    value = dict(fact_ref=str(fact_ref), subject=str(subject), state=state,
                 source_ref=str(source_ref or ''), observed_at=_stamp(observed_at),
                 age_s=None if age_s is None else round(float(age_s), 1),
                 detail=str(detail or ''), raw=raw)
    value.update(extra)
    return value


def unknown(subject, reason, *, source_ref, fact_ref=None, observed_at=None):
    """探测失败的统一出口。

    `reason` 要写清**读不到什么**，不是「不可用」。这两件事必须分清：
    「读不到」是知识缺口，「不可用」是世界事实（F-2 的教训）。
    """
    return fact(fact_ref or (str(subject) + '.unknown'), subject, 'unknown',
                source_ref=source_ref, observed_at=observed_at or time.time(),
                detail='读不到，不能确认：%s' % (reason or '原因未知'))


def brief(value):
    """主上下文用的精简版：只留 state + detail（**不含 raw**）。

    captain 定的：`raw` 是给程序用的，不是给模型看的。
    """
    if not isinstance(value, dict):
        return None
    return dict(fact_ref=value.get('fact_ref'), subject=value.get('subject'),
                state=value.get('state'), detail=value.get('detail') or '')


def brief_all(values, *, limit=8):
    """一组事实的精简版；未知与不可用的排在前面（它们更值得她知道）。"""
    rows = [brief(value) for value in (values or []) if isinstance(value, dict)]
    order = {'unknown': 0, 'offline': 1, 'needs_setup': 2, 'unsupported': 3, 'ready': 4}
    rows.sort(key=lambda row: order.get(row.get('state'), 9))
    return rows[:limit]


# ---------------------------------------------------------------- 已有入口的适配
# 这一节只**读**现有投影，不改它们。每个适配器都只回答一个问题：
# 「这个入口现在是什么状态」，读不到就 unknown。

def _probe(fn):
    """执行一次探测，返回 (值, 失败原因)。异常不外抛——探测失败是常态。"""
    try:
        return fn(), None
    except Exception as error:  # noqa: BLE001 - 探测失败要变成 fact，不是异常
        return None, '%s: %s' % (type(error).__name__, str(error)[:120])


def phone_context_fact(context, *, now=None):
    """`PhoneControl.context()` → 一条手机事实。

    注意 `context()` 用布尔 `connected`；这里翻成词表里的词，
    **不改变它的返回值**。
    """
    now = time.time() if now is None else now
    source = 'life/companion_phone.py#PhoneControl.context'
    if not isinstance(context, dict):
        return unknown('phone', '执行器没有给 phone_context', source_ref=source, fact_ref='phone.link')
    devices = context.get('devices') or []
    if not devices:
        return fact('phone.link', 'phone', 'offline', source_ref=source, observed_at=now,
                    detail='手机最近没有上报过注册，看不到在线设备')
    streaming = [d for d in devices if isinstance(d, dict) and d.get('has_stream')]
    if not streaming:
        return fact('phone.link', 'phone', 'offline', source_ref=source, observed_at=now,
                    detail='手机注册过但没有活动连接（App 不在前台或已断开）',
                    raw=context)
    # 世界事实里带上工作区清单：她不用背 id，也不该靠猜。
    workspaces = [item for item in (streaming[0].get('workspaces') or []) if isinstance(item, dict)]
    detail = '%d 台手机有活动连接' % len(streaming)
    if workspaces:
        detail += '；工作区 ' + '、'.join(
            '%s%s' % (item.get('workspace_id'),
                      ('（%s）' % item.get('package')) if item.get('package') else '')
            for item in workspaces[:4])
    elif streaming[0].get('workspace_state'):
        detail += '；工作区状态 %s' % streaming[0].get('workspace_state')
    return fact('phone.link', 'phone', 'ready', source_ref=source, observed_at=now,
                detail=detail, raw=context)


def channel_fact(name, value, *, now=None):
    """`_channel_facts()` 的一条 → 一条通道事实。

    它本来就带 `state`/`source_ref`/`observed_at`，这里只做词表映射：
    `ACCOUNT_STATES` 的 6 个词收敛到 `STATES` 的 5 个。

    **detail 里必须写明「这是通道，不是某个动作」**：她读到的是一行字，
    看不到 `subject` 字段。而 `channel.qq` 的 `needs_setup` 意思是「这个通道没开」，
    和 `action.dsh` 的 `needs_setup`（「这台机器压根没有」）完全不同，
    混在一起读会误导。
    """
    now = time.time() if now is None else now
    source = (value or {}).get('source_ref') or 'life/companion_awareness.py#_channel_facts'
    label = str(name).upper() if str(name).lower() in ('qq', 'wechat') else str(name)
    if not isinstance(value, dict):
        return unknown('channel:%s' % name, '通道事实不是字典', source_ref=source,
                       fact_ref='channel.%s' % name)
    state = value.get('state')
    mapping = {'connected': 'ready', 'disconnected': 'offline', 'disabled': 'needs_setup',
               'unconfigured': 'needs_setup', 'login_required': 'needs_setup',
               'unknown': 'unknown'}
    if state not in mapping:
        return unknown('channel:%s' % name, '状态词 %r 不在已知词表里' % state,
                       source_ref=source, fact_ref='channel.%s' % name)
    # 每种状态给一句带「这是通道」的话，避免和动作类条目混淆。
    phrasing = {
        'ready': '%s 消息通道已登录可用' % label,
        'offline': '%s 消息通道已开启，但连接断了' % label,
        'needs_setup': ('%s 消息通道没开或没登录' % label
                        if state == 'disabled'
                        else '%s 消息通道已开启，但需要先登录' % label),
        'unknown': '%s 消息通道状态读不到' % label,
    }
    detail = phrasing.get(mapping[state], '%s 消息通道状态是 %s' % (label, state))
    detail += '（这是消息通道，不是某个动作）'
    error = value.get('error')
    if error:
        detail += '；原因：%s' % str(error)[:80]
    return fact('channel.%s' % name, 'channel:%s' % name, mapping[state],
                source_ref=source, observed_at=value.get('observed_at') or now,
                detail=detail, raw=value, account_state=state, enabled=value.get('enabled'))


def town_fact(bridge, *, now=None):
    """`TownBridge` → 一条小镇事实。**读配置判断「有没有配」，读 state 判断「连没连上」。**

    为什么要分开读（2026-11-18 实测）：
      `TownBridge.__init__` 把 `self.state` 设成 `'disabled'`（`life/companion_town.py:172`），
      只有 `start()` → `set_active()` → `connecting` → `_bind()` 成功后才变 `'online'`。
      **所以 `state == 'disabled'` 有两种截然不同的含义，光看它分不出来：**
        * 配置里 `enabled=False` —— 真的没启用
        * 桥刚构造、还没 `start()` —— 还没开始连

      前者是「需要你去开」，后者是「现在不知道」。混成一句话就会说错。
      `config.enabled` / `config.code` 是**配置事实**，任何时刻都可信，用它来定性；
      `state` 只用来判断连接进度。
    """
    now = time.time() if now is None else now
    source = 'life/companion_town.py#TownBridge'
    if bridge is None:
        return unknown('town', '角色上没有 town 桥', source_ref=source, fact_ref='town.link')

    config, failure = _probe(lambda: getattr(bridge, 'config', None))
    if failure is not None:
        return unknown('town', failure, source_ref=source, fact_ref='town.link')

    # `closed` 先判：桥已经 close()，对象已收尾，config/state 都可能是残值。
    # 这时读不到任何有效状态——按 F-2 说 unknown，不能说成 needs_setup（暗示"你去开"），
    # 也不能说成 offline（暗示"连不上"）。
    early_state, _ = _probe(lambda: str(getattr(bridge, 'state', '') or ''))
    if early_state == 'closed':
        return unknown('town', '小镇桥已关闭，现在读不到它的状态（不是配置问题）',
                       source_ref=source, fact_ref='town.link')

    enabled = bool(getattr(config, 'enabled', False))
    code = str(getattr(config, 'code', '') or '').strip()

    # 配置定性在先：没启用 / 没接入码，都是配置项，和连接无关。
    if not enabled:
        return fact('town.link', 'town', 'needs_setup', source_ref=source, observed_at=now,
                    detail='小镇没有启用，是配置项不是网络问题', raw=dict(enabled=enabled))
    if not code:
        return fact('town.link', 'town', 'needs_setup', source_ref=source, observed_at=now,
                    detail='小镇已启用但没有接入码，需要先接入', raw=dict(enabled=enabled))

    state, failure = _probe(lambda: str(getattr(bridge, 'state', '') or ''))
    if failure is not None:
        return unknown('town', failure, source_ref=source, fact_ref='town.link')
    error, _ = _probe(lambda: str(getattr(bridge, '_error', '') or ''))
    detail = '' if not error else '（%s）' % error

    # 配置是好的，接下来只看连接进度。
    mapping = {'online': ('ready', '小镇现在是连着的'),
               'connecting': ('offline', '小镇正在连接，稍后会好；现在还不能派活'),
               'reconnecting': ('offline', '小镇连接断了正在重连；现在还不能派活'),
               'paused': ('offline', '小镇已暂停；恢复后会继续，不是坏了'),
               'error': ('offline', '小镇连接出错'),
               'needs_account': ('needs_setup', '小镇没有可用账号凭据，需要先接入'),
               'invalid_code': ('needs_setup', '小镇接入码无效，需要重新获取'),
               'config_error': ('needs_setup', '小镇配置读不出来')}
    if state in mapping:
        scope, text = mapping[state]
        return fact('town.link', 'town', scope, source_ref=source, observed_at=now,
                    detail=text + detail, raw=dict(enabled=enabled, state=state))
    if state == 'disabled':
        # 配置说启用了、接入码也有，但桥的 state 还是构造初值 —— 还没 start。
        return unknown('town', '小镇配置是好的，但连接还没开始（桥尚未启动），不能确认它现在能不能用',
                       source_ref=source, fact_ref='town.link')
    return unknown('town', '状态 %r 没有对应说法' % (state or '空'),
                   source_ref=source, fact_ref='town.link')


def bilibili_fact(status, *, now=None):
    """`Bilibili.status()` → 一条 B站事实，**区分插件没装 / 没登录 / 掉线**。"""
    now = time.time() if now is None else now
    source = 'life/companion_bilibili.py#Bilibili.status'
    if not isinstance(status, dict):
        return unknown('bilibili', '读不到 B站状态', source_ref=source, fact_ref='bilibili.status')
    if not status.get('available'):
        return fact('bilibili.status', 'bilibili', 'needs_setup', source_ref=source,
                    observed_at=now, detail='B站插件没有安装或路径不对（不是登录问题）', raw=status)
    if not status.get('credential_present'):
        return fact('bilibili.status', 'bilibili', 'needs_setup', source_ref=source,
                    observed_at=now, detail='B站插件在，但没有找到登录凭据（需要先扫码登录）', raw=status)
    if not status.get('connected'):
        return fact('bilibili.status', 'bilibili', 'offline', source_ref=source,
                    observed_at=now, detail='B站插件与凭据都在，但后端进程没连上', raw=status)
    return fact('bilibili.status', 'bilibili', 'ready', source_ref=source,
                observed_at=now, detail='B站后端可用', raw=status)


def self_process_fact(facts, *, now=None):
    """**已废弃：不要用这个适配器，也不要重新加回来。**

    `self_process` 回答的是「我是什么环境」（跑在哪、能不能写自己的源码、改完怎么生效），
    而本层的 `state` 回答的是「这个主体现在能不能干活」。**这是两个不同的问题。**

    硬套会撑坏词表：`can_write_own_source=False` 既不是 `offline`（它在线），
    也不是 `needs_setup`（没有东西要 setup）；填 `ready` 又会让 `ready` 同时表示
    「能派活」和「不能写源码」两种意思——正是今晚 `disabled`/`disconnected`/`offline`
    混用的同一个错误。

    环境事实继续由 `Companion._self_process_facts()`（`life/companion.py`）原样下发，
    不经过本层。原因与判定见 `docs/world-state-audit.md` §4.4。
    """
    raise NotImplementedError(
        'self_process 不是可用性事实，不进 world 事实层；'
        '用 Companion._self_process_facts()，见 docs/world-state-audit.md §4.4')


def dsh_fact(executor, *, now=None):
    """DSH 运行时能不能用 → 一条 `dsh.runtime` 事实。

    **三态，不能压成两态**：
        True  → 运行时路径齐全                → ready
        False → 探测到了，明确不可用          → needs_setup
        None  → 读不到（抛错 / 链上找不到持有者）→ unknown

    `None` 与 `False` 必须分开：`None` 是「不知道」，`False` 才是「本机没有」。
    旧代码（`_offline_reason` 的 dsh 分支）把两者都说成「本机没有可用的 DSH 运行时」，
    正是 F-2 修的那个缺陷的形态。

    **走链逻辑与 `life/companion_awareness.py::_dsh_endpoint` 是同一份实现的两份拷贝。**
    不能反向 import awareness（会成环），所以这里复制；两处都留了指针注释，
    并有测试断言行为一致。**改一边必须改另一边。**
    """
    now = time.time() if now is None else now
    source = 'life/companion_awareness.py#_dsh_endpoint'
    endpoint, failure = _probe(lambda: _dsh_endpoint_probe(executor))
    if failure is not None:
        return unknown('dsh', failure, source_ref=source, fact_ref='dsh.runtime')
    if endpoint is None:
        return unknown('dsh', '读不到 dsh 路径探测结果', source_ref=source, fact_ref='dsh.runtime')
    if endpoint:
        return fact('dsh.runtime', 'dsh', 'ready', source_ref=source, observed_at=now,
                    detail='本机有可用的 DSH 运行时')
    return fact('dsh.runtime', 'dsh', 'needs_setup', source_ref=source, observed_at=now,
                detail='本机没有可用的 DSH 运行时（dsh_root/dsh_node 或入口缺失）')


def _dsh_endpoint_probe(executor):
    """`_dsh_endpoint` 的本地拷贝（见上面的双向指针说明）。

    与 `life/companion_awareness.py::_dsh_endpoint` 逐行等价：
    沿 `original` 链最多走 4 层找 `_dsh_paths`，返回 True / False / None。
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


def body_fact(context, *, now=None):
    """桌面身体形象（小人）能不能做动作 → 一条 `body.link` 事实。

    **和 `computer` 不是一回事**：`computer` 是能不能看屏幕/点鼠标（走 server peer），
    `body` 是能不能让她的小人做动作（走 `_body_context`）。两者是不同的链路，
    不能共用一句话。见 captain 2026-11-18 的提醒。

    **三档，不是两档**（F-2 那条规矩在这里同样是关键）：

    | `_body_context` | 含义 | state |
    |---|---|---|
    | `connected=True` | 桌面桥在推状态 | `ready` |
    | 有 `figure`/`position` 等痕迹但 `connected=False` | 连过，现在断了 | `offline` |
    | 只有 `{'connected': False}`（初始值） | **从没连过** | `unknown` |

    为什么第三档必须是 `unknown`：`life/companion.py:531` 把它初始化成
    `dict(connected=False)`，`update_body()` 只在桌面桥真的推来状态时才覆盖它。
    所以**一个从没连过桌面的进程，和一个刚断开的进程，看到的是同一个 `connected=False`**。
    光凭这个布尔分不开两者——若一律说成 `offline`，就是「不知道」说成「知道」。
    能分开的依据是：桥推过状态就会带上 `figure`/`position`/`motion`/`can_walk` 等字段，
    这些会残留在字典里，即使 `connected` 已经变回 False。
    """
    now = time.time() if now is None else now
    source = 'life/companion.py#_body_context'
    if not isinstance(context, dict):
        return unknown('body', '拿不到 _body_context', source_ref=source, fact_ref='body.link')
    if not context.get('connected'):
        # 有没有「曾经连过」的痕迹？有痕迹才是「断了」，否则是「从没连过」。
        traces = [key for key in ('figure', 'position', 'motion', 'can_walk', 'visible',
                                  'screen', 'name', 'vocab', 'actions')
                  if context.get(key) not in (None, '', {}, [])]
        if not traces:
            return unknown('body', '桌面桥从没有推过身体状态，无法确认小人能不能动',
                           source_ref=source, fact_ref='body.link')
        return fact('body.link', 'body', 'offline', source_ref=source, observed_at=now,
                    detail='桌面身体形象连过但现在没连上，动作发过去没人接收', raw=context)
    walkable = context.get('can_walk')
    figure = str(context.get('figure') or '').strip()
    detail = '桌面身体形象已连上' + ('（%s）' % figure if figure else '')
    if walkable is False:
        detail += '；现在不能走动（can_walk=false），能做的动作受限'
    return fact('body.link', 'body', 'ready', source_ref=source, observed_at=now,
                detail=detail, raw=context)


def computer_fact(peer, *, now=None):
    """桌面客户端（看屏幕 / 点鼠标）→ 一条 `computer.link` 事实。

    数据源是 `CompanionServer.peer`（桌面 websocket 连没连进来），**与 `body` 分开**。
    """
    now = time.time() if now is None else now
    source = 'life/companion_remote.py#CompanionServer.peer'
    if peer is not None:
        return fact('computer.link', 'computer', 'ready', source_ref=source, observed_at=now,
                    detail='桌面客户端已连上，可以看屏幕和点击')
    return fact('computer.link', 'computer', 'offline', source_ref=source, observed_at=now,
                detail='桌面端没有连上（桌面客户端的 websocket 没连进来），'
                       '所以现在既看不到屏幕也不能点击')


def _server_of(core):
    """取执行器背后的 server（`peer` 挂在它上面）。读不到就是 None。"""
    executor = getattr(core, 'executor', None)
    for _ in range(4):
        if executor is None:
            return None
        server = getattr(executor, 'server', None)
        if server is not None:
            return server
        executor = getattr(executor, 'original', None)
    return None


def collect(core, *, executor=None, now=None):
    """把上面能读到的入口一次性盘成一组事实。

    每个入口单独兜底：一处读不到只让那一条是 unknown，不影响其余。

    **不含 `self_process`**：它是环境描述不是可用性状态，理由见 `self_process_fact()`
    的废弃说明。这一处没接是**故意的**，不是漏了。
    """
    now = time.time() if now is None else now
    executor = executor if executor is not None else getattr(core, 'executor', None)
    out = []

    context, failure = _probe(lambda: executor.phone_context()
                              if hasattr(executor, 'phone_context') else None)
    out.append(unknown('phone', failure, source_ref='life/companion_phone.py#PhoneControl.context',
                       fact_ref='phone.link') if failure
               else phone_context_fact(context, now=now))

    bridge, failure = _probe(lambda: getattr(core, 'town', None))
    out.append(unknown('town', failure, source_ref='life/companion_town.py#TownBridge.state',
                       fact_ref='town.link') if failure else town_fact(bridge, now=now))

    backend, failure = _probe(lambda: getattr(getattr(executor, 'original', executor),
                                              '_bilibili', None))
    if failure is not None:
        out.append(unknown('bilibili', failure,
                           source_ref='life/companion_bilibili.py#Bilibili.status',
                           fact_ref='bilibili.status'))
    elif backend is None:
        out.append(unknown('bilibili', '执行器上没有 bilibili 后端',
                           source_ref='life/companion_bilibili.py#Bilibili.status',
                           fact_ref='bilibili.status'))
    else:
        status, failure = _probe(backend.status)
        out.append(unknown('bilibili', failure,
                           source_ref='life/companion_bilibili.py#Bilibili.status',
                           fact_ref='bilibili.status') if failure
                   else bilibili_fact(status, now=now))

    out.extend(channel_facts(core, now=now))
    out.append(dsh_fact(executor, now=now))
    # computer / body 是两条不同链路，分开盘：前者走 server peer，后者走 _body_context。
    peer, failure = _probe(lambda: getattr(_server_of(core), 'peer', None))
    out.append(unknown('computer', failure, source_ref='life/companion_remote.py#CompanionServer.peer',
                       fact_ref='computer.link') if failure
               else computer_fact(peer, now=now))
    context, failure = _probe(lambda: getattr(core, '_body_context', None))
    out.append(unknown('body', failure, source_ref='life/companion.py#_body_context',
                       fact_ref='body.link') if failure
               else body_fact(context, now=now))
    return out


def channel_facts(core, *, now=None):
    """把 `_channel_facts()` 的每个通道翻成事实。读不到整组就返回一条 unknown。

    `_channel_facts` 住在 `life/companion_awareness.py`，是**能力目录里 `channel.*`
    条目的同一个来源**。这里只读它、不改它，避免两处各自判断通道状态。
    """
    now = time.time() if now is None else now
    source = 'life/companion_awareness.py#_channel_facts'
    fn, failure = _probe(lambda: __import__('life.companion_awareness', fromlist=['_channel_facts'])
                         ._channel_facts)
    if failure is not None:
        return [unknown('channel', failure, source_ref=source, fact_ref='channel.all')]
    facts, failure = _probe(lambda: fn(core, getattr(core, 'cfg', None)))
    if failure is not None:
        return [unknown('channel', failure, source_ref=source, fact_ref='channel.all')]
    if not isinstance(facts, dict):
        return [unknown('channel', '通道事实不是字典', source_ref=source,
                        fact_ref='channel.all')]
    rows = [channel_fact(name, value, now=now) for name, value in facts.items()]
    return rows or [unknown('channel', '没有任何通道条目', source_ref=source,
                            fact_ref='channel.all')]
