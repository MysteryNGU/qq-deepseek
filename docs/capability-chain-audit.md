# 能力链路断点审计（只读）

审计对象：「已实现能力 → 可发现说明 → 当前状态 → 实际调用 → 结果回查」五环节。
审计方式：代码逐行核对 + 在云端真实运行环境（`/opt/qq-deepseek`，`Python 3 / .venv-companion`）跑只读复现脚本 + 只读查询生产库 `/var/lib/companion/xiaoyu/data/life.db`。
**本文件不改任何生产代码、不写生产库。** 复现脚本落在 `/tmp/_audit_*.py`（云端），仅调用纯函数与 `__new__` 壳对象，不连网、不启动角色。

## 0. 口径与版本锁定

| 项 | 值 | 证据 |
|---|---|---|
| 审计基线 | 本地仓库 = 云端生产版本（逐文件 md5 一致） | `md5sum` 对比：`companion_awareness.py` `66ad181a…`、`companion_executor.py` `131b7476…`、`tool_contracts.py` `09519b13…`、`capability-policy.json` `2db33424…`、`companion_context.py` `35337670…`、`companion_connections.py` `0633e12b…` 六项全同 |
| 生产进程 | `companion.service` active，`--home /var/lib/companion/xiaoyu --serve --port 8765` | `systemctl cat companion.service` |
| 生产库 | 386 MB，`life.db`，只读打开 | `ls -la /var/lib/companion/xiaoyu/data/life.db` |

行号均指上述基线版本。

---

## 一句话结论

链路不是断在「代码里没有」，而是断在**三处口径不接**：

1. **用法说明（GUIDES）与状态投影（awareness.capabilities）互不相认**——前者决定「会不会告诉你怎么用」，后者决定「会不会告诉你现在能不能用」，两者各自按 `available_actions` 过滤，结果是：能力消失时用法说明一起消失，**但不产生任何一条「为什么没了」的可读条目**（除 dsh 外）。
2. **状态判定用宽口径、派发用严口径**——`phone` 的 `connected` 只看 90 秒内有没有上报，派发却要求有活队列；生产库里有 **38 条 phone failed**，模型从状态上看不出这个落差。
3. **回查写回只认 `succeeded`，而执行器一律返回 `completed`**——结果本身落库了，但「这一步已经有真实回执」这条事实**从未被写进项目**，续行时会让她重读同一份东西。

下面按五环节逐条给判定与证据。

---

## 1) 已实现能力清单

### 1.1 模型能发出的动作只有 13 种，字段只有 7 个

`life/companion.py:247-250`（DECISION）：

```
'actions': {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False, 'required': ['kind', 'project'],
  'properties': {'kind': {'enum': ['command','work','dsh','agent','computer','body','bilibili','phone','town','web','skill','memory','growth']},
                 'project': {'type': 'string'},
                 'command': {...}, 'goal': {...}, 'cwd': {...}, 'op': {...}, 'arguments': {'type':'string'}}}}
```

实测（`/tmp/_audit_runtime6.py`）：

```
=== DECISION action fields the model can emit ===
-> ["arguments", "command", "cwd", "goal", "kind", "op", "project"]
=== DECISION action required ===
-> ["kind", "project"]
```

**判定：通。** 13 种 kind、`op` + `arguments`（JSON 字符串）通道齐全。这是能力链路的入口，没有结构性缺口。

### 1.2 真实可调用清单（按执行器分派入口，附生产库实际调用次数）

分派入口：`life/companion.py:2912-2918`，`executor = town if kind=='town' else body if kind=='body' else self.executor`，然后 `executor.run(action)`（town 走 `execute`）。

| kind | 执行器入口 | op 契约来源 | 生产库实际调用（`companion_actions`） |
|---|---|---|---|
| `command` | `companion_executor.py:168-239` | 直接 `command` 字段；PowerShell `-EncodedCommand` | completed 344 / failed 42 / cancelled 8 |
| `work` | `companion_executor.py:343-383` → `companion_work.py:231-244` | `read/write/edit/list/glob/ls/grep` + `run` 转 command | failed 2 / completed 0 |
| `dsh` | `companion_executor.py:193-239` | `goal` 字段 | completed 1 / failed 5 |
| `agent` | `companion_executor.py:153-167` → `companion_agents.py:1636` | 七操作 `create/send/inspect/wait/cancel/resume/read_artifact`（`companion_agents.py:68`） | failed 4 / cancelled 1 / queued 1 |
| `computer` | `companion_executor.py:137-148` → `companion_remote.py:98` | `companion_computer.py:141-203`，`info/windows/screenshot/zoom/release/click/move/drag/scroll/type/key/focus` | **0** |
| `phone` | `companion_remote.py:99` → `companion_phone.py:117` | `OPS`=`info/screenshot/tap/swipe/text/back/home/recents/open_app`；`WORKSPACE_OPS`=`workspace_open/status/pause/resume/close`（`companion_phone.py:15-16`） | completed 121 / failed 38 |
| `body` | `companion_body.py:55` | `pet_act/pet_move_to/pet_set/pet_quiet/pet_see_self` | completed 61 / failed 18 |
| `town` | `companion_town.py:214` `execute()` | `delegate` / `status` | completed 189 / failed 9 |
| `bilibili` | `companion_bilibili.py:117-148`，`bili_watch/bili_subtitle` 另走 `companion.py:2915-2916` | MCP 工具名透传 | completed 201 / failed 9 / interrupted 3 |
| `web` | `companion_web.py` | `search/open/follow/read_more` | completed 54 / failed 25 |
| `skill` | `companion_skills.py` | `list/read/save` | completed 2 / failed 3 |
| `memory` | `companion_executor.py:111-127` | `search/read` + awareness 扩展 `awareness/capabilities/attention/agent_tasks/attention_set` | completed 14 / failed 5 |
| `growth` | `companion_executor.py:128-132,245-341` | `note/list/revise/drop/log/restore/review` | completed 8 / cancelled 1 |

数据源：`/tmp/_audit_probe.py`（只读 `SELECT json_extract(action,'$.kind') … GROUP BY`）。

**判定：通（清单层面）。** 每条 kind 都有真实执行器入口，且其中 11 条在生产库里有真实成功调用。

### 1.3 两个「已实现但当前不可用」的能力，状态是对的，但原因不可读

- `computer`：生产库 **0 条调用**。代码路径要求 `server.peer is not None`（`companion_remote.py:86`），即桌面客户端 WS 连接。
- `agent`：6 次尝试 **0 次成功**。生产库逐条：`TypeError: AgentManager.inspect() got an unexpected keyword argument 'goal'`、`TypeError: AgentManager.create() got an unexpected keyword argument 'kind'`、`PermissionError: …/agents/shared`（两次）。`companion_agent_tasks` 全部只有 1 行，状态 `interrupted`。

**判定：断。** 见 §4。

---

## 2) 可发现说明

### 2.1 模型看不到 `capabilities()` 的完整投影，只看到摘要

写入口 `life/companion.py:1429-1432`：

```python
awareness = build_awareness(self, state=state, current=current, wake_kind=…, attention=attention)
state['awareness'] = public_view(awareness)
```

`public_view`（`companion_awareness.py:570-572`）剥掉 `_catalogue`/`_tasks`；请求体里的 `capabilities` 是 `capability_summary` 的结果（`companion_awareness.py:553`，实现 `:447-458`）。

实测（`/tmp/_audit_runtime3.py`，`available_actions=['web','command','skill','phone','dsh']`）：

```json
{"total": 8, "counts": {"ready": 6, "needs_setup": 2},
 "ready": ["action.web","action.command","action.skill","action.phone","channel.qq","channel.wechat"],
 "not_ready": ["action.dsh needs_setup（本机没有可用的 DSH 运行时…）",
               "agent.subagent needs_setup（本机没有可用的 DSH 运行时…）"],
 "note": "这里是摘要；细节用 memory op=capabilities 读，每条都有自己的 checked_at 与原因。"}
```

完整条目只能靠 `memory op=capabilities` 取（`companion_awareness.py:713-719`）。实测该条目：

```json
{"capability_ref":"action.phone","category":"action","description":"动作种类 phone",
 "node":"cloud-linux","status":"ready","reason":null,"evidence":["available_actions"],
 "checked_at":"2026-11-17T23:10:25+08:00","schema_ref":"life/companion.py#DECISION.actions"}
```

实测字段全集：`["capability_ref","category","checked_at","description","evidence","node","reason","schema_ref","status"]`——**没有任何一条带参数、限制或示例字段**。

覆盖度实测（`/tmp/_audit_runtime8.py`、`_audit_runtime9.py`，`available_actions` 取真实云端全量 12 个 kind）：

```
available_actions: ['web','command','skill','work','memory','growth','bilibili','phone','town','computer','body','agent']
catalogue refs   : ['action.web','action.command','action.skill','action.work','action.memory','action.growth',
                    'action.bilibili','action.phone','action.town','action.computer','action.body',
                    'action.dsh','agent.subagent','channel.qq','channel.wechat']
kinds with NO entry at all: []
```

12 个 kind 全部有对应条目（`agent` 在 `:347` 被显式排除，改由 `agent.subagent` 顶替）。**能力目录本身是全覆盖的，缺的不是覆盖，是「用不了时」的那一条。** 对照 `available_actions` 缺 `computer/body` 时——见 §2.2，条目与之同时消失。

**判定：半通。** 摘要能回答「哪个能用、哪个不能用、为什么」，但**不能回答「怎么用」**。能力目录设计上就是状态目录，不是用法目录。

### 2.2 「怎么用」的唯一来源是 GUIDES，而它跟能力目录是两条互不相认的通路

说明注入点 `life/companion.py:1509`：

```python
instructions=persona+'\n\n'+METHOD+'\n'+MOTIVATION_INSTRUCTIONS+'\n'+ATTENTION_INSTRUCTIONS+'\n'+tool_instructions(kinds)+'\n'+AGENT_INSTRUCTIONS
```

`tool_instructions` 定义在 `life/companion_context.py:40-41`，`return '\n'.join(GUIDES[k] for k in GUIDES if k in kinds)`——**只注入当轮 `available_actions` 里的 kind**。

实测：

```
=== GUIDES keys ===
["bilibili","body","command","computer","dsh","growth","memory","phone","skill","town","web","work"]
=== tool_instructions([command]) length === 118
=== tool_instructions for unknown kind silently empty === ''
```

`GUIDES` 共 12 条，DECISION 有 13 种 kind，**`agent` 没有 GUIDE**（它的说明在 `companion.py:216-228` 的 `AGENT_INSTRUCTIONS`，无条件注入）。

具体后果（实测，`/tmp/_audit_runtime.py`）：

```
=== awareness catalogue when available_actions includes phone ===
["action.web","action.command","action.skill","action.phone","action.computer","action.dsh","agent.subagent","channel.qq","channel.wechat"]
=== awareness catalogue when phone is disconnected (phone absent) ===
["action.web","action.command","action.skill","action.computer","action.dsh","agent.subagent","channel.qq","channel.wechat"]
```

即 `phone` 掉线时：GUIDES 里 `phone` 整段不注入、能力目录里 `action.phone` 条目直接消失、`reason` 不存在。**模型能观察到的只有「它不在了」，得不到「为什么不在」（除 dsh 之外没有任何 kind 有 needs_setup 原因条目）。**

对照：dsh 是唯一被特殊处理的——`companion_awareness.py:371-374` 无条件登记 `action.dsh`，`:364-370` 在 strict 预算下给 `needs_setup` + 完整中文原因，而 `available_actions` 里同时剔除 dsh（`companion.py:1380-1382`）。

**判定：断。** 「已实现 → 可发现说明」这一环的失败方式不是「没有说明」，而是**说明与状态同生同灭**：能力一断，说明也一起消失，留下的只是一个沉默的缺口。

### 2.3 用法说明自身的完备性问题（实测）

- `memory` 的 GUIDE 明确写了 `op=awareness/capabilities/attention/agent_tasks`，但引导句把主用法写成 `op="search"` / `op="read"`（`companion_context.py:27`），而 `READ_OPS` 里**没有 `search`**（实测 `{"read":["awareness","people","conversations","experience","attention","capabilities","agent_tasks"],"set":["attention_set"]}`），`search` 走 `companion_memory.Lookup`。实测 `'search' in (READ_OPS+SET_OPS)` → `false`。两条通路靠 executor 里的 `if/else` 分流（`companion_executor.py:113-127`），GUIDE 里没有交代这个分叉，但**当前行为是对的**（search 落到 Lookup、capabilities 落到 awareness）。
- `companion_awareness.py:826-844` 定义了 `SUMMARY` 与 `:845-853` 的 `GUIDE`（正是「能力摘要怎么读、怎么补问」的说明），**实测全仓库无任何引用**：`references to awareness SUMMARY/GUIDE: []`。这是「写好了但没接线」的说明。

**判定：半通。** 用法说明基本齐全且有真实例子，但 `awareness.SUMMARY/GUIDE` 完全没注入，属于死代码；`agent` 的说明与 DECISION 字段白名单存在口径冲突（§4.3）。

---

## 3) 当前状态：是实时探测还是静态推断

### 3.1 `available_actions` 的构造链（`companion.py:1371-1382`）

```python
kinds = self.executor.available_kinds()          # :1371
if self.town.config.enabled and self.town.config.code: kinds.append('town')   # :1373-1374
if self._body and self._body_context.get('connected'): kinds.append('body')    # :1375-1376
if spending.policy.enabled and spending_mode(...)=='strict': kinds=[k for k in kinds if k!='dsh']  # :1380-1382
```

各 kind 的判定性质：

| kind | 判定依据 | 性质 |
|---|---|---|
| `work` | `Work.available()`（`companion_executor.py:95`） | 静态（配置/目录存在） |
| `memory`/`growth` | 是否 `attach_repo`/`attach_growth`（`:96-99`） | 静态（接线时一次性） |
| `dsh` | `_dsh_paths()` 四重文件存在性检查（`:31-44`） | 静态（启动期探测） |
| `computer`（服务模式） | `self.server.peer is not None`（`companion_remote.py:86`） | **实时**（WS 连接） |
| `phone`（服务模式） | `self.phone_context()['connected']`（`companion_remote.py:86`） | **实时但宽口径**（见 3.2） |
| `body` | `_body_context['connected']` | **实时** |
| `town` | `config.enabled and config.code` | 静态（配置） |
| `qq`/`wechat` 通道条目 | `available_actions is not None`（`companion_awareness.py:380-385`） | **伪实时**（见 3.3） |

**判定：半通。** 连接类（computer/phone/body）是实时的；能力存在类（work/dsh/memory/growth/town）是静态的，这本身合理，但两者在模型眼里长得一模一样——都是 `status: ready` + `evidence: ["available_actions"]`，没有任何字段区分「刚探测过」和「启动时判断过」。

### 3.2 phone：状态判定宽、派发判定严（实测差异）

`companion_phone.py:43-51` `online_devices()`：`now - last_seen <= RECENT_SECONDS(90)`。
`companion_phone.py:63-82` `context()`：`connected=bool(rows)`，rows 只收 online 且 enabled 且有 operations 的（`:73`）。
`companion_phone.py:145-148` `run()` 派发：

```python
queue=next((q for q,d in reversed(list(self.mobile.subscribers.items())) if d==device),None)
if queue is None: raise ValueError('手机当前没有活动连接；先让它保持在前台，再重试')
```

实测（`/tmp/_audit_runtime3.py`）：

```
=== phone: registered 5s ago (context.connected=True) but NO live stream ===
-> {"phone_context_connected": true, "available_actions_would_include_phone": true,
    "dispatch_result": {"status":"failed","output":"手机当前没有活动连接；先让它保持在前台，再重试","code":"phone_unavailable"}}
=== phone: registered 200s ago (>RECENT_SECONDS=90) with live stream ===
-> {"phone_context_connected": false, "available_actions_would_include_phone": false,
    "dispatch_result": {"status":"failed","output":"需要一台在线且已启用无障碍的手机；多台时指定device_id","code":"phone_unavailable"}}
```

生产库旁证：`phone` completed 121 / **failed 38**（失败率 24%）。

代码注释（`companion_phone.py:19-21`、`:66-69`）承认这是刻意的取舍：「锁屏的手机还会继续上报，必须保持可见」「派发更严——`run()` 需要活队列，所以放宽这里永远不会把动作发给没人接收的手机」。取舍本身站得住，但**模型看不到这个落差**：`phone_context` 里 `has_stream` 只在 devices 行里（`:79`），而 `presence.phone`（`companion_presence.py:178-179`）只给 `connected` + 设备名，`available_actions` 只给 kind 名。

**判定：半通。** 状态是真探测的，但探测粒度和派发粒度不同，且**这个不同没有进入模型可见事实**。

### 3.3 `channel.qq` / `channel.wechat`：`ready` 来自 `available_actions` 非空

`companion_awareness.py:380-386`：

```python
actions_reported = state.get('available_actions') is not None
for channel in ('qq','wechat'):
    catalogue.append(_capability('channel.'+channel,'channel','消息通道：'+channel,
        'ready' if actions_reported else 'unknown', …))
```

只要执行器报了任何动作，**两个通道都标 `ready`**——与 qq/wechat 是否真的登录无关。真实账号事实在另一处：`_self_account()`（`:129-190`），状态取自 `ImManager.snapshot()`，拿不到就按配置退化为 `disabled/login_required/unconfigured/unknown`（`:175-186`）。

对照组：`channel.*` 根本不看 `_self_account` 的 `state`。生产库 `qq.json` 仅 51 字节、`qq-token` 存在但**无 QQ 连接进程**（`ss -ltnp` 只有 8765/8231 两个本机端口），而 `channel.qq` 依旧会是 `ready`。

**判定：断。** 这是「状态由配置静态推断」的最明确一例：条目名是 `channel.qq`，`ready` 的判据却是「别的仓库报了动作」。

### 3.4 桌面/终端

- `computer` 可用性 = `server.peer is not None`（`companion_remote.py:86`）——实时。
- 模型可见的 `state.execution.desktop_connected` = `bool(self._body_context.get('connected'))`（`companion.py:1403-1404`）——**来自 body 桥，不是 peer**。
- `presence.desktop = {connected: _body_context.connected, seen_at: updated_at}`（`companion_presence.py:176-177`）。

代码里没有任何地方把 `peer` 与 `_body_context.connected` 对齐。生产库 `body` completed 61 / failed 18，失败样本 `{"status":"failed","output":"桌面身体尚未连接"}`——说明这两个信号确实会分叉。

**判定：半通。** 状态是真探测，但同一件事有两个不同来源、模型看到的是另一个来源。

---

## 4) 实际调用：从模型动作到执行器是否有真实通路

### 4.1 F2 类缺陷复现：`agent` 的参数契约与 DECISION 白名单冲突

`AGENT_INSTRUCTIONS`（`companion.py:216-228`，**无条件注入**）告诉模型：

> `create 要 goal（做什么、在哪做、怎样算做完）、node（dsh/work/echo）、deliverables、acceptance；`
> `之后用 inspect{agent_id} 看状态与验证，wait{agent_id,wait_s:0} …`

但 DECISION 只允许 7 个字段（实测见 §1.1），`agent_id/node/deliverables/acceptance/wait_s/message_id/artifact_id` **全部不在白名单**：

```
=== DECISION has a top-level agent_id field on an action? === false
=== DECISION has node/deliverables/acceptance? === []
=== DECISION has wait_s/message_id/artifact_id? === []
=== top-level agent_id -> schema === rejected: additionalProperties:false
=== create with top-level node/deliverables/acceptance -> schema === rejected: additionalProperties:false
=== create via arguments ===
{"goal":"写一份清单","node":"work","deliverables":["a.md"],"acceptance":["文件存在"],"op":"create","project":"p1"}
```

**判定：半通。** 唯一合法载体是 `arguments` 字符串，`_action_fields`（`companion_agents.py:2409-2440`）能正确解包。但说明里写的 `inspect{agent_id}` 字面形式**会被 schema 拒绝**，模型必须自己推断「要放进 arguments 的 JSON 里」。这不是凭空的担心——生产库的失败记录正是字段流通过程中出错的形态：

```
TypeError: AgentManager.inspect() got an unexpected keyword argument 'goal'
TypeError: AgentManager.create() got an unexpected keyword argument 'kind'
```

`dispatch()`（`companion_agents.py:1657-1673`）把 `fields` 全量 `**` 展开给 `inspect(agent_id, *, changes, messages)`（`:1220`）与 `create(goal, *, deliverables, acceptance, workspace, node…, **kwargs)`（`:816`）。`goal` 混进 `inspect`、`kind` 混进 `create`，都会变成 TypeError 而不是「参数无效」。

根因在 `_action_fields`（`:2415-2419`）：白名单里同时收了 `'goal'` 和 `'kind'` 之外的顶层键，并且**没有按 op 过滤**——`goal` 是 create 的合法参数，却会被原样传给 inspect。实测：

```
=== agent op=inspect WITHOUT agent_id ===  RAISE AgentError: agent_id_required
=== agent op=inspect WITH agent_id + extra goal ===  OK: {"op":"inspect","agent_id":"companion-agent_x","goal":"看看进度"}
```

最后一个 `{"op":"inspect","agent_id":…,"goal":…}` 会一路走到 `self.inspect(agent_id, goal='看看进度')` → TypeError。**这就是 F2 类型缺陷在 agent 通道上的现存形态。**

### 4.2 `phone`：逐条守卫齐全，但守卫的错误码单一

`companion_phone.py:117-168` 的校验顺序完整（op 合法性 → target → workspace 约束 → device 唯一性 → 设备是否声明该 op → 字段白名单 → 坐标/包名/时长）。实测三类拒绝：

```
op=workspace_open 但设备只声明 info/screenshot/tap -> {"status":"failed","output":"手机没有提供该操作能力","code":"phone_unavailable"}
无活队列 -> {"status":"failed","output":"手机当前没有活动连接；…","code":"phone_unavailable"}
注册超 90s -> {"status":"failed","output":"需要一台在线且已启用无障碍的手机；多台时指定device_id","code":"phone_unavailable"}
```

**判定：通。** 派发通路真实存在且守卫严谨。唯一瑕疵：五种不同原因共用一个 `code='phone_unavailable'`，按 code 机器判别不可行（但 `output` 是人可读的，模型能读）。

生产库旁证 `phone` 的真实成功回执（`workspace_status`、`display_id=16`、`session_id`、`coordinate_frame`）说明这条链路确实跑通过。

### 4.3 `phone` 工作区的 op=`info` 与 workspace 的冲突（生产库直接可见）

```
{'op':'info','status':'failed','res':'{"status":"failed","output":"未知工作区操作：info","target":"workspace",…,"code":"workspace_unknown"}'}
```

`info` 在 `OPS` 里（`companion_phone.py:15`），不在 `WORKSPACE_OPS` 里（`:16`）。带 `target=workspace` 时手机端把它当成工作区操作，报 `未知工作区操作`。GUIDE（`companion_context.py:35`）写的是「`info{}` 读界面」+「target:"workspace" 表示动作发生在副屏」，**没有说明 `info` 不能带 workspace**。这是说明与实现的口径差，不是代码缺能力。

**判定：半通。**

### 4.4 `work` 的 `run` 是委托而非实现

`companion_executor.py:347-357`：`op=='run'` 直接转成 `kind='command'` 再 `await self.run(command)`。所以 `work.run` 与 `command` 是同一通路。生产库 `work` 只有 2 条、全 failed：`不存在：/opt/qq-deepseek/skills/computer.md`、`PermissionError: … '/opt/qq-deepseek/notes'`。

**判定：通（能力层面），但零成功样本。** 「零成功」不是断点，是权限与根目录配置问题。

### 4.5 无任何环节校验 `kind ∈ available_actions`

实测全仓库 `available_actions` 的 34 处引用中，**没有一处把这些用作派发前的准入校验**（`available_kinds` 只在 `companion.py:1371` 与 `companion_remote.py:84-86` 被读取来构造清单）。`_validate`（`companion.py:1862-1886`）只做「这个 kind 需要 op 吗」「arguments 是 JSON 对象吗」「command/goal 非空吗」。

后果：`available_actions` 只是**建议**，不是**闸门**。模型即使看到 `phone` 不在清单里，发出去仍会进 `_worker` 分派（`:2912-2918`），只是必然失败。这本身可以辩护（「静默藏起来等于假装没有这个能力」的注释在 `:1377-1379`），但要注意：**它让 2.2 的「说明随状态消失」失去了兜底**——说明没了，调用仍可达。

**判定：半通。** 通路存在，闸门不存在，说明与闸门都不保护模型。

---

## 5) 结果回查

### 5.1 回执确实回到主认知（两条通路，一条有 bug）

落库 + 回主认知：`companion.py:2937` `self._settle(row, result)`，`:2951-2976` 结果轮 `_cognize`。`_settle`（`:2986-3018`）写 `companion_actions.status/result`、ingest `tool_result` 事件、`continuity.record_action`、`followthrough.after_result`。

**判定：通。** 生产库 1169 条 `companion_actions`，`tool_result` 事件与 `action_id` 一一对应（实测最近 5 条 payload 都带 `$.action_id`）。

### 5.2 「这一步已经有真实回执」从未生效——`succeeded` vs `completed`

`companion.py:2995`：

```python
if status=='succeeded':
    self.tasks.ensure(self.scope, row['project_ref'], None,
        last_receipt=dict(action_id=row['id'], at=time.time(), output=…))
```

而执行器返回的 status 一律是 `'completed'`/`'failed'`（`companion_executor.py:226`、`:270`、`:289`，`companion_work.py:241`，`companion_phone.py:179`）。`'succeeded'` 只出现在 `companion_agents.py:94` 的**映射表**里（`'succeeded': 'completed'`），从不是 `_settle` 看到的值。

实测（`/tmp/_audit_runtime5.py`，用真实 `TaskService`）：

```
status=completed  last_receipt=False  step_done(companion.py:2648-2649)=False
status=succeeded  last_receipt=True   step_done=True
```

下游 `companion.py:2647-2661`：

```python
receipt=project.get('last_receipt') or {}
step_done=bool(isinstance(receipt.get('at'),(int,float)) and receipt['at']>=(project.get('next_step_set_at') or 0))
…
elif step_done: wake_text = '这件事上一步已经做过并拿到了真实回执：…'
else:          wake_text = '继续已保存的项目：…下一步：…'
```

生产库全量验证：`companion_actions` 的 `status` 只出现过 `completed/failed/cancelled/interrupted/queued`，**`succeeded` 计数恒为 0**（首查 `995/160/10/3/1`，10 分钟后重查 `996/160/10/3/1`——产线在跑，只有 completed 在涨，`succeeded` 始终 0）。同时 `last_receipt` 全仓库只有 3 处读写（`companion.py:2647`、`:2999`、`companion_followthrough.py:239`），唯一写入点就是这条永不成立的分支。

**判定：断。** 结果确实落库了（数据层是通的），但「有回执」这个**语义**从未写回项目，续行永远走 `else` 分支，让她重读同一份东西。这正是「结果回查」环节最典型的半途而废：回执有，回执的**结论**没接上。

### 5.3 `sent` / `rendered` / `unknown` 分得很好，但没进主认知

`companion_delivery.py:4-13` 定义了完整状态链 `queued → sending → sent → rendered`，旁支 `unknown`（「结果未知，不能改写成失败」）。实现完备：`:163 mark_sent`（`allowed=(QUEUED,SENDING,UNKNOWN)`）、`:169 confirm_render`（`allowed=(QUEUED,SENDING,SENT,UNKNOWN)`，迟到的回执仍有效）、`:221 reconcile`（超时降 `unknown`）、`:236 reconcile_on_start`（重启一律 `unknown`）、`:244 expire_unknown`。

生产库实测：

```
=== delivery states ===   ('rendered',504) ('sent',41) ('cancelled',11) ('unknown',2)
=== delivery rendered ever ===  (558, 504)
```

**判定：通（状态机），断（可见性）。** `summary`/`pending` 只在 `snapshot()` 的 `observability` 里（`companion.py:3053-3054`），那是 **owner 面板**用的。

请求体（`_messages()`，`:1278-1578`）里出现的 `delivery` 全部是**发送目标**，不是送达状态——实测逐个核对：`state['default_delivery']`（`:1401`）= `route['reply_delivery']`（`:1275-1276`）；`decision['delivery']`（`:235`，DECISION 字段）是模型自己填的目标数组；`:1513` 的 `delivery=m['delivery']` 是历史消息里存的目标。**没有一个字段携带送达账本里的 `state`（`sent/rendered/unknown`）。**

实测确认：`_messages` 函数体内 `delivery_ledger` 出现 0 次。模型看不到自己上一句是 `sent` 还是 `rendered` 还是 `unknown`。

对照：`current_state.pending_expression`（`companion_presence.py:146-158`）确实让模型看到「已提交但没有确认送达的话」，说明这条链路**设计上想过**要告诉模型；但 `rendered` 这个最强的确认信号没有进去。

实测（`/tmp/_audit_runtime7.py`）：`_messages` 分段内 `delivery_ledger` 出现 0 次。

### 5.4 `completed` / `failed` 区分（动作回执）

`_settle` 原样存 `result['status']`，`result_view`（`companion.py:166-199`）明确区分：`completed/succeeded` → 「完成了」；`failed` → 走失败分支。`interrupted`（`:2922-2923`）写着「行动被停止，不能据此推断已经完成」。

实测存储值（两次快照）：`completed 995→996 / failed 160 / cancelled 10 / interrupted 3 / queued 1`；`SELECT COUNT(*) … status='succeeded'` 两次都是 `0`。**判定：通。**

但有三个值的语义不对齐：`computer` 会产出 `yielded` / `partial`（`companion_computer.py:192-194`），`phone` 会产出 `interrupted`（`companion_phone.py:179`），而消费者只认 `{completed,succeeded}`（`companion_continuity.py:127-128`、`companion_motivation.py:168`、`companion.py:1054`）。`yielded`/`partial` 在三处都被当成「非成功」。

**判定：半通。** 主词表（completed/failed/interrupted）对齐，边缘词表（yielded/partial）没有对齐，会把「部分完成」读成「没成功」。

### 5.5 失败原因的可读性

- `phone`：`phone_unavailable/phone_busy/phone_timeout/phone_offline` + 中文 `output`，可读。
- `agent`：`code` 是 Python 异常类名（`TypeError`/`PermissionError`），`reason` 是异常原文（`AgentManager.inspect() got an unexpected keyword argument 'goal'`）。**模型看得到，但那是给工程师看的**，不是「你需要补什么」。
- `computer`：简版部署下无回执（0 次调用，无法评估）。

**判定：半通。**

---

## 6) 五环节判定汇总

| 环节 | 判定 | 关键证据 |
|---|---|---|
| 1 已实现能力清单 | 通 | 13 kind / 7 字段（`companion.py:247-250`）；11 kind 有生产成功样本 |
| 2 可发现说明 | **断** | 摘要无参数/示例（实测字段集）；说明与状态同生同灭（实测 catalogue 对比）；`awareness.SUMMARY/GUIDE` 零引用 |
| 3 当前状态 | **半通** | `channel.qq/wechat` 由「别的仓库报没报动作」决定（`companion_awareness.py:380-386`）；phone 宽判定/严派发（实测）；desktop 两套来源未对齐 |
| 4 实际调用 | **半通** | agent 参数契约与 DECISION 白名单冲突（实测 schema 拒绝 + 生产 TypeError）；无 `kind ∈ available_actions` 闸门 |
| 5 结果回查 | **断** | `_settle` 只认 `succeeded`、执行器只产 `completed`（实测 step_done 恒 False）；delivery 状态机完备但不进请求体 |

**链路断点定位：** 断裂集中在「状态 → 说明」的衔接（2+3）与「回执 → 续行」的衔接（5）。「模型能不能发出动作」（1）和「动作能不能到执行器」（4）基本通；真正的损失发生在**能用了却不知道、用过了却记不住**。

---

## 7) 给下游（认知补进机制）的靶点

按「改动点落在现有机制内」排序，每条都对应上面的实测证据。**本节只列靶点与判据，不含实现。**

| # | 靶点 | 落点 | 判据 |
|---|---|---|---|
| A | 回执结论写回项目 | `companion.py:2995` 的 `=='succeeded'` 改为认 `completed/succeeded` | 实测 `step_done` 在一次 completed 后变 True；续行 wake_text 走 `:2657` 分支 |
| B | 通道状态用真实登录事实 | `companion_awareness.py:380-386` 的 `channel.*` 改读 `_self_account()['state']` | 无 QQ 连接时 `channel.qq` 不为 `ready` |
| C | 能力消失时留下原因 | `capabilities()` 为「已实现但当前不在 available_actions」的 kind 补 `needs_setup` 条目（照 dsh 的 `:364-374` 做法） | 断开手机/桌面后，`memory op=capabilities` 能读到对应 needs_setup + reason |
| D | phone 状态与派发口径一致 | `phone_context`（`companion_phone.py:80`）或 `presence.phone`（`companion_presence.py:178`）区分 `connected` 与 `dispatchable` | 实测「注册在窗口内但无活队列」时，模型可见事实里出现不可派发的信号 |
| E | agent 说明与字段白名单对齐 | `AGENT_INSTRUCTIONS`（`companion.py:216-228`）改成 `arguments` 形态；`_action_fields`（`companion_agents.py:2415-2419`）按 op 过滤 `goal`/`kind` | `inspect` 带 `goal` 不再 TypeError；按说明写的动作能过 schema |
| F | delivery 状态进主认知 | `_messages()` 的 state 里补上一轮 delivery 的 `sent/rendered/unknown` | 模型能分辨「已发出但未知」与「端上已呈现」 |

「项目能力两层」的第二层（qq-deepseek 软件本身的结构/源码位置/构建部署）不在本审计范围内，需另立条目；本条目的五环节只覆盖第一层（她作为小鱼的执行能力）。

---

## 附录：复现命令

```bash
# 版本锁定
md5sum /opt/qq-deepseek/life/{companion_awareness,companion_executor,tool_contracts,companion_context,companion_connections}.py \
       /opt/qq-deepseek/life/capability-policy.json

# 只读能力/调用统计（不写库）
.venv-companion/bin/python /tmp/_audit_probe.py
.venv-companion/bin/python /tmp/_audit_probe2.py

# 只读复现（纯函数 + __new__ 壳，不连网、不启角色）
PYTHONPATH=/opt/qq-deepseek .venv-companion/bin/python /tmp/_audit_runtime.py
PYTHONPATH=/opt/qq-deepseek .venv-companion/bin/python /tmp/_audit_runtime3.py
PYTHONPATH=/opt/qq-deepseek .venv-companion/bin/python /tmp/_audit_runtime5.py
PYTHONPATH=/opt/qq-deepseek .venv-companion/bin/python /tmp/_audit_runtime6.py
PYTHONPATH=/opt/qq-deepseek .venv-companion/bin/python /tmp/_audit_runtime7.py
PYTHONPATH=/opt/qq-deepseek .venv-companion/bin/python /tmp/_audit_runtime8.py
PYTHONPATH=/opt/qq-deepseek .venv-companion/bin/python /tmp/_audit_runtime9.py
```

脚本本体同时保存在本地仓库 `.review-server/_audit_*.py`（不在生产代码路径内）。

**审计快照时刻：2026-11-17 23:03–23:15 (+08:00)。** 生产库是活库，动作计数在审计过程中有增长（`completed` 995→996）；所有计数类结论均注明为快照值。结构性结论（`succeeded` 恒为 0、`step_done` 恒 False、`channel.*` 恒 ready）不受计数变化影响，因为它们的判据是代码而非计数。

**未覆盖项（本审计未验证，不得当作已确认）：**
- `computer` 通道的生产可用性：生产库 0 次调用，无法评估回执质量；只能从代码路径确认入口存在（`companion_remote.py:98` → `companion_computer.py:141`）。
- `wechat` 通道：未做真实登录验证；「`ready` 与登录无关」是由 `companion_awareness.py:380-386` 的判据直接推出的，并已用三种 `available_actions` 取值实测。
- 桌面 `peer` 与 `_body_context.connected` 的分叉频率：只从失败样本 `桌面身体尚未连接` 得知两者会分叉，未统计比例。
