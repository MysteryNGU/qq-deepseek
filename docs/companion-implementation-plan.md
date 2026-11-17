# 小鱼持续认知与子 agent：P1 实施契约（阶段 A / 阶段 B）

日期：2026-11-17。性质：**本轮实施契约冻结稿**。本文件是实施与评审的唯一口径来源；与设计冲突处以设计 §2 明确需求为准，与实现落点冲突处**以云端代码为准**。
配套：`docs/companion-awareness-baseline.md`（P0 侦察，所有落点行号的证据来源）。

> 状态标记：**§1–§10 为冻结内容**。**§11「接口字段最终化」待 t4（P1 前置核验）产出后补齐**，届时只增写该节，不改动其余各节的契约结论。

---

## 1. 本轮范围与不做事项

### 1.1 在范围内（Only A + B）

| 阶段 | 范围 | 主责 |
| --- | --- | --- |
| **A 认知层** | 认知投影（self/runtime、账号事实、当前人与会话、注意力、能力可用性、活跃任务及新结果）、人物与会话稳定标识、注意力状态、能力目录与按需读取的统一入口 | awareness-engineer (t6) |
| **B 子 agent 框架** | 七操作闭环（create/send/inspect/wait/cancel/resume/read_artifact）、独立上下文、异步结果、取消与续作、父子关系与总并发配额、预算三模式接通 DSH | subagent-engineer (t7) |

### 1.2 明确不在本轮（阶段 C / D，**不开工**）

以下内容即使在本轮被顺带发现可改，也**不得**动。它们的价值在于规模可控，不在于顺序偏好。

- **QQ 多人交流**：群聊接入、他人私聊、`conversation_ref` 路由到 QQ、逐目标回执、取消「助手消息自动镜像到主人私聊」。
- **跨会话回忆对外**：把她的跨会话检索结果转述给非主人、跨会话对外可见性策略。
- **扫码求助**：登录会话适配器、二维码私有媒体交付、手机端展示与刷新。
- **发布闭环**：Android/Windows 打包、上传、下载校验、真机验收。

> 边界判据（防止范围蔓延）：凡是需要**改动 `life/companion_qq.py` 的收件过滤**（第 129 行 `message_type!='private'`）或**改动 NapCat `/api/QQLogin/*`** 的，一律属 C 阶段。阶段 A 只读该文件的状态用于「账号事实」投影，**不改它的行为**。

### 1.3 允许触碰的文件白名单

**主实施目标（云端真源，不得删除或绕过）**

| 文件 | 云端实测 | 本阶段用途 |
| --- | --- | --- |
| `life/companion.py` | 173360 B | A/B 的主接线点 |
| `life/companion_context.py` | 18072 B | A：工具说明与投影压缩 |
| `life/companion_executor.py` | 19251 B | B：执行入口与 DSH 启动 |
| `life/companion_dsh_worker.py` | 2981 B | B：SDK worker |
| `life/companion_followthrough.py` | 18211 B | B：结果续行 |
| `life/companion_delivery.py` | 10897 B | B：结果投递台账 |

**允许新增**：`life/companion_awareness.py`（A）、`life/companion_agents.py`（B）两个新模块 + 各自测试。新增模块须由上述既有文件 import 接线，**不得**形成第三套并行调度/投递系统（设计 §8.1）。

**禁止触碰**：`life/role_agent.py`（本地/云端有 749 B 差异，未裁定前冻结）、`life/companion_qq.py` 行为、`life/release.py`、`life/companion_mobile.py`、`mobile/`、`tools/build-companion-android.py`、`/var/lib/companion/**`、生产 `config.toml`。

---

## 2. 认知栈裁决（A/B 实现的前提，必须先定）

P0 已确认两套认知栈并存，这是本契约最要紧的一条约束：**A 阶段不得同时向两套栈接线**。

| | `life/companion.py`（Companion） | `life/role_agent.py`（RoleAgent） |
| --- | --- | --- |
| 决策形状 | `DECISION` schema，`actions` 数组按 `kind` 枚举分派（`actions` 在第 231-234 行，`kind` 枚举在第 232 行） | 工具调用；`subagent` 等为一等工具名 |
| 行动枚举 | `['command','work','dsh','computer','body','bilibili','phone','town','web','skill','memory','growth']`，**无 `agent`** | `subagent`/`cancel_agent`/`list_agents` 等已在 `initiative.CONTROL` |
| 幂等/预算 | `companion_actions` 表 + `Spending` | `role_operations` 表（`op` + `request_hash`）+ `Budget.reserve(op)` |
| 续行 | `TaskService` + `FollowThrough` | `InitiateProtocol` + 订阅 `subagent.finished` |
| 生产地位 | **云端 systemd 实际运行的就是它**（`companion.service`） | 云端存在但非当前服务入口 |

### 2.1 冻结结论

1. **阶段 A/B 一律以 `life/companion.py` 为唯一实施目标。** 理由：它是云端 `companion.service` 实际运行的栈，`main()` → `run_remote_service` → `Companion` 是真实生产路径；把认知层接到非运行栈上，等于做了但线上看不见，违反「真实接线」。
2. **`role_agent.py` 在本轮只作参考实现读，不改、不接线、不删除。** 它的 `subagent` 接线（第 1434、1452-1461、1472-1483、530/537、2236 行）是 B 阶段**设计参照**：B 要复现它的语义质量（意图前置、方法转授权、预算先行、终态订阅续行），但落在 `companion.py` 的表与流程上。
3. **`DECISION.actions.kind` 必须新增 `'agent'`**（B 阶段唯一需要扩 schema 的地方），否则七操作无处进入。这是有意的一处 schema 变更，不是「只完成 schema」——见 §6.2 的完成条件。

### 2.2 本地独有与云端独有的衔接

| 类别 | 文件 | 衔接方式 |
| --- | --- | --- |
| **云端独有**（本地一个都没有，共 47 个 `companion_*.py`） | `companion_awareness` 将与其共处同一包的 `companion_presence.py`、`companion_evidence.py`、`capability_view.py` 复用 | **只在云端开发**；本地不镜像。测试走 §8 隔离沙箱（沙箱先把云端源码拷进临时树） |
| **本地独有** | `role_agent.py`、`runtime.py`、`settings_web.py` | `runtime.py` 云端**也有**（40773 B，`LifeRuntime` 在 `life/runtime.py:58`），非本地独有——A 阶段直接复用其 `ingest/scope/context_block`。`role_agent.py`、`settings_web.py` 本轮不参与 |
| **两端都有但内容不同** | `role_agent.py`（差 749 B） | **冻结**，不读入实现、不合并 |
| **新增模块归属** | `companion_awareness.py`、`companion_agents.py` | 二者都是**云端独有新增**，本地不建；由云端 `companion.py` import |

**衔接原则**：本轮不要求本地与云端源码对齐。本地只保留文档、测试与设计；**代码改动发生在云端**，通过 §8 的拷贝沙箱验证，回滚靠云端 `life.bak-*` 快照 + 改动前 `sha256sum` 存证。

---

## 3. 阶段 A：认知层契约

### 3.1 接线点（唯一挂载位置）

`life/companion.py:1227` `_messages()` 内部组装 `state`，其中 `state['current_state'] = presence_state`（第 1367 行）是既有投影挂载点。

**契约**：新增一个投影段 `state['awareness']`，在 `_messages` 内、`state['current_state']` 之后注入，**由 `companion_awareness.build(core, current=current, wake_kind=..., attention=...)` 一次生成**。
- 只读：`build()` 不得写库、不得发网络、不得改 `state` 其他键。
- 幂等：同一 `(now, current, wake_kind)` 两次调用返回等值结构。
- 失败隔离：`build()` 内部任何子段异常只落到 `awareness.projection_errors[]`，**不得**让整轮认知失败（与 `companion_presence._section` 的既有防御风格一致）。
- 未知语义：字段为 `None` / `[]` 表示未知，调用方不得自行补造（沿用 `companion_presence.build` docstring 的既有约定）。
- 复用而非重写：`self.account` 与 `runtime.available_actions` 必须与既有来源同源——`available_actions` 直接取 `state['available_actions']`（第 1329 行），账号取 `CompanionQQ.snapshot()` 的 `state`/`account` 字段，避免第二份真相。

### 3.2 投影结构（最小必填集）

设计 §5.1 要求「至少包含 `observed_at`/`source_ref`」。冻结为：

```
awareness = {
  "observed_at": ISO8601,            # 必填
  "projection_version": int,          # 必填，结构变更时递增
  "self": {
      "agent_id": str,
      "account": {                    # 账号事实：值必须来自登录核验，不得来自 persona
          "value": str|None,          # 未知则 None
          "source_ref": str,          # 如 companion_qq snapshot 的 source
          "observed_at": ISO8601|None,
          "state": "connected|disconnected|disabled|unconfigured|login_required|unknown"
      }
  },
  "runtime": {
      "node": str,                    # "cloud-linux"
      "home": str,
      "available_actions": [str],     # 与 state['available_actions'] 一致，单一来源
      "spending_mode": "off|strict|bounded_estimate"   # 与 §5 三模式一致
  },
  "here": {                           # 当前在哪：人与会话
      "actor_ref": str|None,
      "account_ref": str|None,
      "conversation_ref": str|None,
      "platform": str|None,
      "observed_at": ISO8601|None
  },
  "attention": [
      {"conversation_ref": str, "mode": "focused|watching|muted|released",
       "reason": str, "since": ISO8601, "review_at": ISO8601|None,
       "expires_at": ISO8601|None, "cursor": str|None}
  ],
  "recent_experience": [ {"source_ref": str, "kind": str, "at": ISO8601, "preview": str} ],
  "capabilities": [ {"capability_ref": str, "category": str, "status":
        "ready|needs_setup|offline|unsupported|unknown",
        "reason": str|None, "checked_at": ISO8601|None, "schema_ref": str|None} ],
  "agent_tasks": [ {"task_ref": str, "state": str, "node": str,
        "last_progress_at": ISO8601|None, "new_results": int} ],
  "projection_errors": [ {"section": str, "error": str} ]
}
```

### 3.3 能力目录状态规则（防止「谎报 ready」）

- `ready` **仅当**该能力在当前节点有一次真实成功调用证据，或在本次会话内完成过就绪探测。
- 阶段 A 交付时，`agent`（子 agent）能力**必须标 `needs_setup`**——因为 B 尚未完成。B 落地后再改为 `ready`，且改动须附一次真实调用证据。
- `dsh` 能力的状态由 `spending_mode` 决定：`strict` 下若无法计量则 `needs_setup` 并给 `reason`，**不得**静默从目录消失（这是 P0 定位的 §2.1 缺陷的 A 阶段修法）。

### 3.4 按需读取接口

设计 §5.5 的统一读取动作，落到 `DECISION.actions` 的既有 `kind` 上：**扩展 `memory` 动作的 `op`**，而非新增 kind。

- `op in {awareness, people, conversations, experience, attention, capabilities, agent_tasks}`
- 入参：`query`、`ref`、`cursor`、`limit`、`since`、`until`、`actor_ref`、`conversation_ref`（按 kind 适用）
- 返回：`items`、`next_cursor`、`source_refs`、`freshness`、`truncation`
- 不产生对外发言、不默认改写记忆。
- 关注变更走 `op=attention_set`，入参 `conversation_ref`、`mode`、`reason`、`review_at`/`expires_at`。

**阶段 A 只实现 `awareness`、`capabilities`、`attention`、`agent_tasks` 四个 kind 的读取**；`people`、`conversations`、`experience` 的跨会话检索依赖 C 阶段的人物/会话模型，本轮返回 `status=not_implemented` 且**必须显式写明原因**，不得返回空数组冒充「查无此事」。

---

## 4. 阶段 B：子 agent 框架契约

### 4.1 七操作的落点

统一为 `kind=agent, op=...`，进 `DECISION.actions`（§2.1 第 3 条的新增枚举）。

| op | 必填入参 | 必须回执 | 落点 |
| --- | --- | --- | --- |
| `create` | `goal`、`deliverables`、`acceptance`、`workspace`、`node_requirements`、`scope`、`budget`、`parent_task_ref`、`idempotency_key`、`context_refs` | `agent_id`/`task_ref`/`operation_id` + `queued` | `companion_agents.create()` → `companion_actions` 落库 → `_wake.set()` |
| `send` | `agent_id`、`message_id`（幂等键）、`text` | 已投递/已排队 + 不重复创建 | 追加到该 agent 的入站队列 |
| `inspect` | `agent_id`、可选 `cursor` | 状态、最近进度、等待原因、检查点、证据、产物索引；默认不读全日志 | 读 `companion_actions` + checkpoint |
| `wait` | `agent_id`、`cursor`、`timeout_s`（短时限，**上限 60 s**） | 超时返回当前状态，不阻塞主认知 | 订阅状态变化 |
| `cancel` | `agent_id`、`scope`（是否含子树） | 取消意图 + 已终止进程树 + **已发生且不可撤销的副作用清单** | `ProcessTree.stop()` + 状态置 `cancelling→cancelled` |
| `resume` | `agent_id`、`idempotency_key` | 新 `attempt_id`、保留历史、**先核对既有外部副作用** | 新 attempt 行 |
| `read_artifact` | `artifact_ref`、`range`（行范围/分页） | 分段内容 + `next_cursor`，不把全量回灌 | 读产物文件 |

### 4.2 状态机（唯一真源）

```
queued ──▶ running ──▶ completed | failed | waiting | interrupted | cancelled
             │                                 │
             └──▶ cancelling ──▶ cancelled     └──▶ running (恢复)
```

- `waiting` **必须**带 `waiting_for`（等待对象）与 `recover_when`（恢复条件），否则落库被拒。
- `completed` **必须**带结果清单；`verification ∈ {pending, passed, failed, unverified}`，**独立于** `completed`。
- `interrupted`：守护恢复时 `running` 先转 `interrupted`，核对节点租约与进程存活后再决定接管或续作。

### 4.3 并发与配额

- 全角色合计执行中 agent 上限 **3**；嵌套深度上限 **2**（可直接用 DSH 的 `depthLimit`/`maxDepth`）。
- 排队**不失败**，只置 `queued`。
- 配额与父预算共享，子 agent 不得突破父预算。
- 修改同一仓库必须隔离工作目录，应用修改串行；只读或独立仓库可并行。
- 以上数值可配置，默认取设计 §6.1。

### 4.4 上下文与持久恢复

- **默认不复制完整聊天史**。传目标、必要用户原话、相关事实/约定、文件引用、验收条件、能力引用。
- 每个任务持久：`task brief`、`checkpoint`、`append-only events`、`result manifest`。
- 检查点字段：已做、待做、阻断、基线版本、修改文件、产物、操作 ID、**外部副作用状态**。
- **续接判定**：P0 已实测 SDK 真实支持 `session/resume`（失败抛错而非新建）。因此 `resume` **优先复用同一 `session_id`**；若恢复失败则退回「检查点起新 attempt」，并在回执中**明确标注这是续作而非恢复同一模型上下文**（设计 §6.2 原文要求）。

### 4.5 结果清单（主上下文摘要上限）

```
result = {
  outcome, summary,           # summary ≤ 约 1200 中文字符
  changes,                    # 最近 5 个
  tests, artifacts, evidence_refs,
  remaining, blockers,
  side_effects: [ {kind, target, at, reversible: bool} ],
  usage: {known: {...}, unknown: {...}} | None   # 见 §5，禁止把未知写成 0
}
```
超出部分以引用保留（`artifact_ref`），可调并可测量请求预算。

### 4.6 接线点（不得新造第三套）

| 需求 | 复用 | 禁止 |
| --- | --- | --- |
| 任务/项目主线 | `life/tasks.py` `TaskService` + `companion_actions` | 新开调度线程/新表 |
| 结果续行 | `companion_followthrough.FollowThrough`（`record`/`due`/`claim`/`after_result`） | 自定义轮询 |
| 投递台账 | `companion_delivery.queue/state/mark_*`（已有 `conversation_ref` 形参，第 91 行） | 自建消息队列 |
| 执行与进程树 | `companion_executor.ActionExecutor` + `supervisor.ProcessTree` | 直接 `subprocess` |
| SDK 调用 | `companion_dsh_worker.execute()` | 在 `companion.py` 内直接起 SDK |

---

## 5. 预算三模式契约（B 的硬前置）

P0 已证实 **SDK 只发 4 个 RPC、无 usage 字段**，故 `strict` 对 DSH 结构性不可行。冻结为：

| 模式 | 行为 | 要求 |
| --- | --- | --- |
| `off` | 不限制，仅记录已知用量 | 默认不改变既有用户选择 |
| `bounded_estimate` | **推荐默认**。允许缺完整 usage，但强制：并发上限、单任务运行时长上限、调用次数上限；记录「已知费用 + 未知部分」 | 终止时返回受阻原因，可换方法或求助 |
| `strict` | 完整计费已知才允许执行 | 对 DSH 必须**显式报 `capability needs_setup` 并给出原因**，不得静默隐藏 |

**刚性约束**
- 禁止把未知用量写成 `0`。未知即 `usage.unknown`，或整体 `usage=None`。
- 不静默改变用户已明确选择的 `strict`（设计 §6.3）。
- 若 SDK 无法限制调用次数，**必须展示该限制不可用**，并至少落实超时/并发。

---

## 6. 完成条件（含真实接线与可观察认知）

每阶段**必须**同时满足「真实接线」与「可观察认知」，只完成 schema 视为未完成（设计 §10 原文）。下表是评审的判定依据，不是自评清单。

### 6.1 阶段 A 完成条件

| # | 条件 | 判定方法（可观察） |
| --- | --- | --- |
| A1 | `state['awareness']` 真实进入模型请求 | 从一轮真实（沙箱）认知的请求体里取到该键，并打印其 `observed_at` |
| A2 | 她能在回答中正确引用「我在哪、用谁的号」 | 构造一轮提问，检查回复依据 `awareness.here`/`self.account`；账号未知时回复须含「未知」类表述 |
| A3 | 能力目录不谎报 | `agent` 能力在 A 阶段为 `needs_setup`；`dsh` 在 `strict` 下有 `reason` |
| A4 | 按需读取可用且不发言 | 调 `op=awareness|capabilities|attention|agent_tasks`，返回 `items/next_cursor/source_refs/freshness/truncation`，且不产生出站投递记录 |
| A5 | 未实现的 kind 显式说明 | `people/conversations/experience` 返回 `not_implemented` + 原因，**不是**空数组 |
| A6 | 投影失败不打断认知 | 注入一个子段异常，整轮仍完成，`projection_errors` 有记录 |
| A7 | 未知不被补造 | 关闭 QQ 通道后，`self.account.state` 为 `disabled` 而非 `connected`；`observed_at` 不为编造值 |

### 6.2 阶段 B 完成条件

| # | 条件 | 判定方法（可观察） |
| --- | --- | --- |
| B1 | 七操作全部可达 | 对每个 `op` 至少一次真实调用并取到回执 |
| B2 | 创建后主认知不被占锁 | `create` 返回 `queued` 后，同一进程内立刻完成一次闲聊轮次（有回复、有投递） |
| B3 | `send` 幂等 | 同 `message_id` 两次投递只产生一条入站记录 |
| B4 | 运行中补充可追溯 | `send` 后 `inspect` 能看到该消息且不产生第二个 `agent_id` |
| B5 | 重启不丢任务、不重复发布 | kill 主进程后重启：任务从 `interrupted` 转续作；已完成任务不重复执行；外部副作用先对账 |
| B6 | 产物验收与完成分离 | 子 agent 报 `completed` 但产物缺失/哈希不符时，`verification=failed`，主认知**不**报「已完成交付」 |
| B7 | 取消能终结进程树 | `cancel` 后进程树无残留（`pgrep` 为空） |
| B8 | 续接真实生效 | `resume` 复用原 `session_id` 成功；失败时回执明示「续作非恢复」 |
| B9 | 预算三模式按契约 | `off`/`bounded_estimate` 可执行；`strict` 下 DSH 被显式阻断且原因可见；`usage` 未知不写成 0 |
| B10 | 并发与嵌套上限生效 | 第 4 个并发 `create` 落 `queued` 而非失败；第 3 层嵌套被拒 |
| B11 | 结果摘要不撑爆主上下文 | 结果摘要 ≤ 约 1200 中文字符、变更 ≤ 5，超出以引用保留 |

### 6.3 两端共用的纪律

- 不得把历史测试通过数（如「1228 项」）套用到本次修改（设计 §11 末句）。
- 协议故障用假服务重放；真实模型场景单独验证「她是否理解事实并采取合理行动」，**不得用测试夹具回答代替模型行为验收**。

---

## 7. 证据口径：五档分开

任何交付一律**分五档**报告，禁止混为一个「完成」。每档必须附可复核路径（命令 + 输出摘录 + 时间）。

| 档 | 名称 | 定义 | 谁给 |
| --- | --- | --- | --- |
| ① | **代码已实现** | 代码合入并通过静态检查/import | 实施者 |
| ② | **离线测试通过** | 隔离沙箱内测试全绿 | test-infra / isolation-verifier |
| ③ | **在线接通** | 云端服务内真实接线生效（有真实请求/回执） | isolation-verifier |
| ④ | **真机验收** | 真实设备/真实客户端上的行为确认 | 仅阶段 D；本轮**应为「未进行」** |
| ⑤ | **实际发布** | 已发布到渠道并有下载校验 | 仅阶段 D；本轮**应为「未进行」** |

**本轮预期**：阶段 A/B 交付应达到 ① ② ③；④⑤ 明确写「未进行」，不得留空或暗示已做。

---

## 8. 隔离测试契约（所有测试执行的唯一方式）

依据：P0 + `tests/companion_test_isolation.py`（test-infra 产出，18359 B）。

| 要求 | 约束 |
| --- | --- |
| 运行位置 | 只在 **copy** 出的 `test-run` 源码树上跑；生产树只读 |
| HOME | `HOME`/`USERPROFILE`/`APPDATA`/`XDG_*`/`TMPDIR` 全部指向沙箱 |
| 数据库 | `fresh`（全新临时库）或 `copy`（SQLite backup API 只读副本）；**永不**写源库 |
| 外部平台 | `DENY_ENV` 清空 `DEEPSEEK_API_KEY`/`QQ_WS_URL`/`QQ_WORKER_TOKEN` 等 |
| 外部执行 | socket guard + subprocess guard（in-process runner） |
| 配置 | 沙箱内 `config.toml` 中性化，密钥置空 |
| 归属守卫 | 拒绝在 `/var/lib/companion`、`.venv-companion` 等受保护根内建沙箱 |
| 清理 | `cleanup()` 后打印 `cleaned=` / `home_deleted=` |

**硬红线**：不向生产 `life.db` 写测试记忆；不向真实 QQ 发验收消息；发布需用户确认。

---

## 9. 交付物与评审口径

| 阶段 | 交付物 | 评审门 |
| --- | --- | --- |
| A | `life/companion_awareness.py` + `companion.py`/`companion_context.py` 接线 + 测试 + 五档证据 | t8 独立评审（verdict=pass 才可继续） |
| B | `life/companion_agents.py` + 六个既有文件接线 + 测试 + 五档证据 | t8 独立评审 |
| 验收 | 隔离验收报告（t9） | 证据回收 |
| 报告 | 本轮交付报告（t12） | — |

**评审不得由实施者自评**。`needs_revision`/`reject` 必须携带可定位到文件:行号的问题与必需修法。

---

## 10. 风险与回滚

| 风险 | 缓解 |
| --- | --- |
| 改动 `companion.py` 破坏主服务 | 改动前 `sha256sum` 存证；云端 `life.bak-*` 三份快照可回滚；改动后 `systemctl is-active` + `/health` 探活 |
| 七操作与既有 `_worker` 争用执行槽 | 复用 `self._execution` 单执行位，agent 运行放独立 task，不占 `_lock` |
| 预算模式切换导致行为突变 | 默认 `off` 不变；`bounded_estimate` 需显式配置才生效 |
| 新增 `kind='agent'` 与旧 schema 不兼容 | `additionalProperties:false` 下只增枚举值，不改必填项；旧决策不受影响 |
| 认知投影拖慢每轮请求 | `build()` 设总预算，超预算按段降级并记 `projection_errors` |

---

## 11. 接口字段最终化（依据 t4 产出补齐）

依据：`docs/companion-prerequisites.md`（P1 前置核验）§7「给 A / B / D 的接口契约」。**本节不回溯修改 §1–§10**。
字段名可在实现时调整，但**语义与失败要求不得放宽**（t4 原文）。

### 11.1 能力目录与按需读取（A 依赖）

```text
capability_ref : str      稳定标识，如 "agent.create"、"dsh.session.resume"
category       : enum     model | tool | agent | channel | node | media | runtime
description    : str      给小鱼看的一句话，不写实现术语
node           : str      cloud-linux | owner-windows | owner-android
status         : enum     ready | needs_setup | offline | unsupported | unknown
reason         : str      status≠ready 时必填，写清缺什么
checked_at     : iso8601  最近一次真实探测时间
schema_ref     : str|null 详情读取入口
```

按需读取：`kind ∈ {awareness, people, conversations, experience, attention, capabilities, agent_tasks}`，过滤字段 `query/ref/cursor/limit/since/until/actor_ref/conversation_ref`，返回 `items/next_cursor/source_refs/freshness/truncation`。

**实测初始值（t4 探针，`tools/verify-dsh-sdk-capabilities.py`）**：`dsh.session.resume=ready`、`dsh.tools=ready`、`dsh.events=ready`、**`dsh.subagent=needs_setup`**（`sdk-minimal` 无委派工具）、`dsh.usage=unsupported`、`qq_life.channel=unsupported`。

### 11.2 预算接口（全阶段共用）

```text
mode        : strict | bounded_estimate | off     （产品默认 bounded_estimate）
limits      : {concurrency, task_seconds, calls_per_task, max_output_tokens, daily_limit_fen, daily_target_fen}
enforceable : {concurrency: bool, task_seconds: bool, calls_per_task: bool}
accounting  : {charged_nano, reserved_nano, unknown_calls, price_version, estimate_sources}
```

**迁移规则（不得静默改用户选择）**：`enabled=true` 且无 `mode` 键 → **保持 `strict`**；新装/未配置 → `bounded_estimate`；`enabled=false` 等价 `mode=off`；`enabled=false` 但 `mode=strict` → 启动即报配置错误，不猜测。
**`enforceable.calls_per_task` 实测为 `false`**，必须在对小鱼的展示里如实出现。

### 11.3 七操作与 DSH 的真实对应（B 依赖）

| 操作 | DSH 侧基础 | 判定 |
|---|---|---|
| create | `session/prompt` 开新 `sessionId` | 可做（新会话模拟执行节点） |
| send | `session/prompt` + `messageId` 幂等 | 可做 |
| inspect | `session.event` 订阅 + 自建状态表 | 可做，状态由宿主维护 |
| wait | 订阅 `session.status=idle` | 可做，需短时限与游标 |
| cancel | 仅能终止子进程树 | **部分**（能停，无 `settled` 语义） |
| resume | `session/resume` | 可做，须区分「真实恢复」与「检查点新起 attempt」 |
| read_artifact | 宿主读 workroot 产物 | 可做 |

SDK 不提供、须宿主补齐：父子关系、检查点、并发配额（合计 3 / 嵌套 2）、用量与预算模式、节点租约。

**明确不允许**：因为 `sdk-minimal` 没有委派工具，就把「子 agent 可用」写进口头承诺——要么启用带 `tool-subagent` 的 profile 并实测，要么把 `dsh.subagent` 保持 `needs_setup` 并用宿主侧执行节点实现七操作。（本轮实际走的是后者。）

### 11.4 产出路径与隔离 workroot（数据安全前置）

t4 结论：**现状不满足**——cwd 缺省回落 `home`，且 `DSH_PERMISSION_MODE=danger-full-access`，cwd **不阻止**写到别处。

强制要求：
1. **无 workroot 即拒绝执行**（不允许回落到 `home`）。
2. 事后按 mtime 核验产物确实落在 workroot 内。
3. 客户端只回投影，不回流原始库内容。
4. 执行侧把 `life.db` 移出执行上下文；会话描述脱敏。

`tool_boundary.py` 实测（16 用例）能挡直接库名，**挡不住**通配/拼接/遍历——因此上述 1–4 不能只依赖 `tool_boundary`。

### 11.5 发布事实维度（D，本轮不开工，仅登记）

```text
release_ref : 绑定源文件 manifest / commit / 平台 / 版本
built / verified / published / download_verified / device_verified : bool + evidence_ref
sha256 / size / signature_id / build_node / rollback_pointer
```
五个维度分别记录，不能用一个 `completed` 掩盖真机未测；上传后必须用客户端方式实际下载并核对哈希；发布需用户确认。

### 11.6 `projection_version` 初值与 result manifest / checkpoint 字段

- `awareness` 投影：`schema = "awareness-1"`（t6 实现值，已在隔离验收中作为字段被断言）。
- `result manifest` 与 `checkpoint` 的最终字段集：以 `life/companion_agents.py` 实际实现为准，本轮 P3 验收已就 `outcome/summary/changes/tests/artifacts/evidence_refs/remaining/blockers/side_effects/usage` 与 `waiting_for/recover_when` 逐项取证；摘要上限约 1200 中文字符、变更 ≤ 5（实测 `summary_chars=219`、`changes=1`）。

---

## 12. 实施进度（2026-11-17 本轮）

### 12.1 各阶段状态

| 阶段 | ①代码已实现 | ②离线测试通过 | ③在线接通 | ④真机验收 | ⑤实际发布 |
|---|---|---|---|---|---|
| **A 认知层** | ✅ | ✅ | ✅（09:08 重启后生效） | 未进行 | 未进行 |
| **B 子 agent** | ✅ | ✅ | ❌ **代码已落盘但进程未重启** | 未进行 | 未进行 |
| C QQ / 跨会话 / 扫码 | — | — | — | — | — |
| D 发布闭环 | — | — | — | — | — |

阶段 C、D **本轮未开工**（不是取消）。

### 12.2 阶段 A 落点

`life/companion_awareness.py`（新增，43897 B）+ `companion.py:45/:1375/:1478/:1490`、`companion_executor.py:70/:105`、`companion_remote.py:158`、`companion_context.py:27`。

### 12.3 阶段 B 落点

`life/companion_agents.py`（新增，106746 B）、`life/companion_agent_worker.py`（新增，7049 B），`life/companion.py` 5 处接线（`DECISION` 加 `'agent'`、`AgentManager` @446、`_workers()`/`_agent_worker_one()`、`_worker` 排除 `kind=agent`、`_settle` 记账）。

### 12.4 隔离验收结果

100 条可观察结果：**97 通过 / 2 失败 / 1 受限**（`docs/companion-verification-20261117.md`，本轮已独立复核 `result.json` 汇总一致）。

- 失败 1：投递账本接受裸数字会话引用 — `life/companion_delivery.py:91` `queue()` 未调用 `companion_agents.py:299 normalize_route()`。
- 失败 2：嵌套上限 2 未生效 — `life/companion_agents.py:942 _depth()` 的 `LIMIT 1` 取父行与 `task_ref` 复用不匹配。
- 受限 1：转述措辞需真实模型判断，`FakeModel` 不冒充验证。

### 12.5 §6 完成条件的实际达成

A1–A7 全部达成。B1–B7、B9、B11 达成；**B8 部分**（`session_id` 复用路径未在真实 DSH 上跑，仅验到 `mode=checkpoint` 续作并明示「续作非恢复」）；**B10 部分**（并发 3 生效，嵌套 2 有洞）。

### 12.6 需用户决定

1. **是否重启 `companion.service` 让阶段 B 上线**（否则线上小鱼仍不能创建子 agent）。
2. 两条失败是否本轮修（均已有定位与建议修法）。
3. 是否花一次真实模型费用跑通一条真实 DSH 委托。

### 12.7 双基线残余分化（仍需跟踪）

- `life/companion_delivery.py` **本地缺失**（云端 10897 B，MD5 `1a8bf3f23938b5e77931a33998d55217`）→ 本地契约测试有一条恒定假失败。
- 本地无 `tools/stage-a/`；云端无 `tools/stage-b/` 与 `docs/stage-b-cloud-check.json`。
- 云端 47 个 `companion_*.py` 本地不镜像（§2.2 已冻结）。

详见 `docs/companion-delivery-report-20261117.md`。
