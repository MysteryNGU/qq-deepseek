# World 状态来源审计

日期：2026-11-18。**只读评估，未改任何代码。**
回答的问题：现有 world 状态一共有几处来源、各自什么形状、有没有统一的可能性。

判定依据：云端 `life/companion.py` md5 `b9f69dea97f4c61e9b91ca4c9aa21991`（3369 行，t13 落地后），
以及仓库里 `life/` 下各投影模块。**本文行号指这两份，动手前先 `md5sum` 现取。**

---

## 0. 结论先说

用户提的四条约束，现在的完成度差别很大：

| 约束 | 现状 | 差在哪 |
|---|---|---|
| **1 单一数据源** | **部分做到** | 手机、通道、账号已经做到了；**但是同一个手机有 2 套、同一个通道有 2 套** |
| **2 状态由数据派生** | **大部分做到** | `_channel_facts` 是正面样板；**但手机"90 秒"写死在两句文案里** |
| **3 统一输出形状** | **没做到** | 9 个投影各自发明字段名；只有 3 个带齐 `state`+`source_ref`+`observed_at` |
| **4 可追溯** | **部分做到** | `awareness` 系带齐了；`town` / `observerability` / `bilibili.status` 基本没有 |

**一句话**：架构方向上是对的（这一层本来就是为「不猜」建的），**但缺一个统一的形状约定**，
导致同一件事在不同投影里长得不一样，读的人（包括她自己和前端）必须逐个适配。

**统一不需要重写**：最小做法是加一个**适配层**（§4），把已有投影归一成同一种
「事实」形状，不动任何现有调用方。

---

## 1. 一共有哪些「读 world 状态」的入口

我按「谁在回答关于世界的什么问题」把它们分了三层。

### 1.1 直接投影层（给角色/前端看的整包形状）

| 入口 | 文件 | 回答什么问题 | 主要调用方 |
|---|---|---|---|
| `awareness.build()` | `life/companion_awareness.py` | 我在哪、用谁的号、能用什么、手上有几件事 | 主角色每轮 |
| `presence.build()` | `life/companion_presence.py` | 我此刻的状态、情绪、活动、约定 | 运行详情 |
| `build_observability()` | `life/companion_observability.py` | 用量、项目进展、近期回执与错误 | 运行详情（`runtime=1`） |
| `town.snapshot()` | `life/companion_town.py` | 小镇连没连上、跑过几轮 | 前端 `meta`、运行详情 |
| `im.snapshot()` | `life/companion_im.py` | QQ / 微信两个通道现在什么状态 | 手机设置页、`_channel_facts` |
| `life_view.build()` | `life/companion_life_view.py` | 「今天」和「往日」的叙事视图 | 手机生活页 |

### 1.2 设备/连接事实层

| 入口 | 文件 | 回答什么问题 |
|---|---|---|
| `phone.context()` | `life/companion_phone.py:63` | 现在有哪些手机可以派活 |
| `phone_link()` | `life/companion_phone_view.py:23` | 手机链路现在什么状况、为什么连不上 |
| `_channel_facts()` | `life/companion_awareness.py:137` | 每个通道的真实登录状态 |
| `_self_account()` | `life/companion_awareness.py:230` | 我用的是哪个号 |

### 1.3 零散事实

| 入口 | 文件 | 回答什么问题 |
|---|---|---|
| `_self_process_facts()` | `life/companion.py:1392` | 我跑在哪、源码在哪、改完怎么生效 |
| `bilibili.status()` | `life/companion_bilibili.py:36` | B 站后端可不可用 |
| `executor.available_kinds()` | `life/companion_executor.py` | 这台机器现在能干什么 |
| `phone_gap()` | `life/companion_phone_view.py:54` | 连不上的那一句原因 |

**共 15 个入口，分布在 9 个文件里。**

---

## 2. 每处的形状：字段名、状态词、有没有时间戳和来源

「能不能统一」的核心证据是这张表。**同一列里出现不同的字体，就是不一致。**

| 入口 | 状态字段名 | 状态词表 | 时间戳 | 来源引用 | 判定 |
|---|---|---|---|---|---|
| `awareness._runtime` | `available_actions` + `autonomy_active` | — | `observed_at` ✓ | `source_ref` ✓ | **规范** |
| `awareness._here` | `wake_kind` / `platform` | `timer/touch/project/user/external` | `observed_at` ✓ | `source_ref` ✓ | **规范** |
| `awareness._channel_facts` | **`state`** | `ACCOUNT_STATES`（6 值） | `observed_at` ✓ | `source_ref` ✓ | **规范，可作模板** |
| `awareness._self_account` | `state` | 同上 | ✓ | ✓ | **规范** |
| `awareness.capabilities` | **`status`** | `ready/needs_setup/offline/unsupported/unknown` | `checked_at` | `schema_ref` | 次规范（键名不同） |
| `phone.context()` | **`connected`** + `enabled` | 布尔 | ✗ | ✗ | **不一致** |
| `phone_link()` | **`available`** + `connected` + `online` | 布尔 | `last_seen_seconds` | ✗ | **不一致** |
| `im.snapshot()` | **`state`** | `logged_in/waiting_scan/needs_login/disabled/connecting/disconnected/closed/error/invalid` | ✗ | ✗ | 词表自成一派 |
| `town.snapshot()` | **`state`** | 自有取值 | ✗ | ✗ | 无时间戳 |
| `presence.build()` | `presence` / `body` 子结构 | 各子系统自定义 | `generated_at` ✓ | ✗ | 半规范 |
| `build_observability()` | 按用途分组 | 无统一状态词 | `generated_at` | ✗ | 无状态概念 |
| `bilibili.status()` | **`available`** + `connected` + `credential_present` | 布尔 | ✗ | ✗ | **不一致** |
| `_self_process_facts()` | `can_write_own_source` 等 | 布尔 | ✗ | `source_ref` ✓ | 半规范 |
| `available_kinds()` | 列表 | kind 名 | ✗ | ✗ | 无状态 |
| `phone_gap()` | 自由文本 | — | ✗ | ✗ | 无结构 |

### 2.1 三类不一致，逐个说

**① 同一个布尔判断，三个不同的键名**

```
phone.context()      -> connected
phone_link()         -> available + connected
bilibili.status()    -> available + connected + credential_present
awareness             -> state（字符串，不是布尔）
```

前端读「手机在不在」时，按入口不同要试 `connected` / `available` / `state`。

**② 状态词表各说各的**

- `awareness`：`ACCOUNT_STATES`（`connected/disconnected/disabled/unconfigured/login_required/unknown`）
- `capabilities`：`STATES`（`ready/needs_setup/offline/unsupported/unknown`）
- `im.snapshot`：`logged_in/waiting_scan/needs_login/disabled/connecting/disconnected/closed/error/invalid`

**三套词表里 `disabled` / `disconnected` / `offline` 语义重叠但不等价。**
capabilities 的 `offline` 表示「探测到了、确实不可用」，而 `unknown` 表示「读不到」——
这个区分很关键（正是 F-2 修的那个 bug），但只有 capabilities 这一套词表表达了它。

**③ 只有 3/15 带齐了 `source_ref` + `observed_at`**

`awareness` 全家做到了，这正是它可信的原因——每条事实都能回答「你凭什么这么说、什么时候说的」。
其余投影大多没带，所以「这个状态是几秒前的还是昨天的」看不出来。

---

## 3. 哪些是「读数据源」，哪些是「写死」

这是用户最关心的那条。我逐个查了。

### 3.1 做对的（读数据源派生）

| 位置 | 怎么做的 |
|---|---|
| `_channel_facts`（`life/companion_awareness.py:137`） | 登录核验快照优先，配置文件兜底，都拿不到就是 `unknown`。**文档字符串明说「通道是否在线，只能问通道自己」** |
| `_self_process_facts`（`life/companion.py:1392`） | `source_root` 从 `__file__` 推，拿不到写 `null`，**注释明说「不写死一定是 /opt/qq-deepseek」** |
| `_offline_reason`（`life/companion_awareness.py:464`） | 三档：探测到不可用 → 说具体原因；探测抛错 → `unknown`；**不许把「读不到」写成断言** |
| `phone.context()` | 在线 = 注册窗口内，不是靠直播流猜 |

**这一层是今晚修出来的一批（F-2 / F-4 那条线），方向完全正确。**

### 3.2 写死的（有具体行号）

| 写死的东西 | 位置 | 问题 |
|---|---|---|
| **「90 秒」写进提示文案** | `life/companion_awareness.py:482`、`:673` | 常量 `RECENT_SECONDS=90` 在 `companion_phone.py`，**文案里又手写了两个 90**。常量一改，文案就撒谎 |
| `town` → `needs_setup` | `life/companion_awareness.py:503` | 直接断言「没启用或没有保存接入码」，**没有去读 `town.config` 来确认** |
| `bilibili` → `needs_setup` | `life/companion_awareness.py:506` | 断言「没有登录态」，没读凭证文件的实际可解析状态 |
| `dsh` → `needs_setup` | `life/companion_awareness.py:508` | 「本机没有可用的 DSH 运行时」——**这条至少可读 exec 器的探测结果** |
| `unit='companion.service'` | `life/companion.py:1409` | 单元名写死。`source_root` 都推出来了，单元名却没推 |

**`_dispatch_caveat` 里有一句很好的注释**：`life/companion_awareness.py:669-671`
> 「清单说能用，但我这边看不到任何在线设备：这句必须跟事实走，不能照抄『注册过但没有活动连接』——那是另一种情形。」

说明写这段的人**已经意识到写死文案会撒谎**，但只在这一处做了，`:482`/`:673`/`:503`/`:506`/`:508` 还没做。

### 3.3 「读数据源但读的是二手数据」

`town` 与 `bilibili` 的真正状态其实**在数据源里是有的**：
- `town.config` 有 `enabled` / `code`
- `bilibili.storage` 有凭证文件
- `available_kinds()` 里有 `town` / `bilibili`

所以第 5 条不是「没法派生」，是**「派生了却不读，直接断言」**。这一类比上一类好修。

---

## 4. 统一的最小侵入做法

**不改现有任何投影的返回值**（那会牵动前端、手机、测试）。
加一层**适配器**：把 15 个入口归一成同一种「事实」形状。

### 4.1 统一的形状

```python
dict(
    fact_ref   = 'phone.link',            # 稳定标识，不是描述
    subject    = 'phone',                 # 说的是谁
    state      = 'offline',               # 只许用既有词表（见 4.2）
    source_ref = 'life/companion_phone.py#PhoneControl.context',
    observed_at= '2026-11-18T09:12:31+08:00',
    age_s      = 3.0,                     # 可选：距测量过了多久
    detail     = '手机注册过但没有活动连接（App 不在前台）',
    raw        = {...},                   # 原始投影，原样保留
)
```

**关键点：`raw` 原样保留。** 适配器只**新增**一层统一视图，
调用方想用原来的字段照旧可以。这是「不重写、只加适配」的落点。

### 4.2 词表怎么统一（不引入新词）

不要发明第四套词表，**以 `capabilities` 的 `STATES` 为准**，因为它已经把
最重要的区分表达出来了：

| 统一词 | 含义 | 已有的同义词 |
|---|---|---|
| `ready` | 探测过、确实可用 | `connected=True` / `available=True` |
| `offline` | **探测到了**，确实不可用 | `disconnected` / `closed` |
| `needs_setup` | 配置缺失，不是网络问题 | `unconfigured` / `needs_login` |
| `unsupported` | 这台机器没有这个能力 | — |
| `unknown` | **读不到，不敢断言** | 布尔 `None` |

映射时要**保住 `offline` 与 `unknown` 的区别**——这正是 F-2 的教训。

### 4.3 落地顺序（按收益/风险比）

| 步 | 做什么 | 风险 |
|---|---|---|
| 1 | 加 `life/world_facts.py`（计划中，尚未创建），只有「形状 + 映射表 + 组装函数」 | 零（新文件） |
| 2 | 先接 **phone 两个入口**（最不一致的一对） | 低 |
| 3 | 再接 **channel + account**（已经是规范形状，只做字段改名） | 低 |
| 4 | 把 5 处写死文案改成读数据源（§3.2） | 中（会给模型下发不同文字，需验收） |
| 5 | 前端/手机逐个切到统一视图 | 按界面来 |

**第 4 步是唯一会改变「她看到什么」的一步**，也是用户最关心的那条（状态由数据派生）。
建议单独做一次，配可解释性一起验收。

---

## 5. 这套东西和 t14（唤醒可解释性）的关系

**t14 的 `_starvation_snapshot` 应该直接用 §4 的事实形状**，否则它会成为第 16 个各说各话的投影。

具体：饥饿条目天然就是一条事实：

```python
dict(fact_ref='queue.agent_action', subject='agent_action', state='starved' 或 'ready',
     source_ref='companion_actions#kind=agent', observed_at=..., age_s=2169.0,
     detail='3 条 kind=agent 动作未被认领，最久 36 分钟')
```

但 `starved` **不在既有词表里**。所以要么：
- **A（推荐）**：`state` 仍用既有词表（`offline`/`unknown`），饥饿另用一个布尔 `starved=True` 字段
- B：给词表加 `starved`，但那要改所有消费方

选 A：加字段不改词表，风险最低。

---

## 6. 我给用户那个问题的直接回答

> 是不是保证了架构的清晰合理？所有 world 的东西都是同样的数据来源，所有输出都是一样？

**数据来源：大部分是同一处，但不是全部。** 15 个入口里，手机和通道各有两套并行实现，
商户名对不上但数据其实是同一份。`town` / `bilibili` / `dsh` 三处直接写死了结论，没读数据源。

**输出形状：不是一样的。** 三个不同的布尔键名、三套状态词表、只有 3/15 带齐来源与时间戳。

**但这不代表架构错。** 方向是对的——从数据派生、读不到就说 `unknown` 这套原则，
在 `awareness` 已经完整落地了。**缺的是一个形状约定和一次归一，不是重写。**

---

## 7. 本次评估没有验证的部分

1. **只读了源码，没有实测各投影的真实输出。** 我可以拉 `/mobile/state` 和 `/rpc`
   实际打一遍对比字段，但那是写操作风险（要带 token），本次没做。
2. **前端/手机实际读了哪些字段**没逐个核对，只看了 `life/` 侧。
3. **`presence.build()` 的子结构**（`body`/`affect`/`mood`/`motivation`…）只看了外层，
   没逐个展开它们的字段名。
4. 桌面端（`companion_desktop.py`）与手机端各自维护的投影是否重复，没查。
