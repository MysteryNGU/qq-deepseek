---
description: "面向让进程外 SDK 客户端在 DeepSeek Harness 运行时中打开会话并驱动 agent 的部署的 stdio JSON-RPC 服务插件。"
kind: "package-reference"
---

# @deepseek-ai/dsh-sdk-jsonrpc-server

[English](README.md) | 中文

## 概述

`dsh-sdk-jsonrpc-server` 通过 stdio 服务 SDK 协议格式（wire format），使进程外客户端能够驱动 harness agent（智能体）：它为每个 `sessionId` 打开一个会话、把用户提示词排入队列，并把每个会话事件与 agent 状态转换流式发回客户端。把它作为 `jsonrpc` 插件挂载到 Loader 组合中；外围插件树提供其余一切——agent、模型适配器、持久化与工具。Stdout 只承载 JSON-RPC 帧，因此部署不得组合 stdout logger。它通过 dispose（资源释放）根运行时并以 0 退出应答 `shutdown`；EOF 与信号退出归 app bin 负责。

## 目录

- [使用本包](#use-this-package)
- [理解实现](#understand-the-implementation)
- [进一步探索](#further-exploration)
- [模型体验](#model-experience)
- [已知限制与延期工作](#known-limitations-and-deferred-work)
- [开发备注](#dev-note)

-----

<a id="use-this-package"></a>
## 使用本包

`session/resume` 在初始化后接受持久化的 `{ sessionId }`，待其 Agent 就绪后返回 `{}`。它通过 `agents.resume()` 加载历史、修复最后一个被中断的轮次，并保留存储的起始目录和当前有效目录，同时应用初始化时选定的模型路由。它不发送新输入。会话缺失、不可读、不兼容或被占用时，请求失败且不创建替代会话。对正在恢复的标识发送提示词时，会等待恢复完成；shutdown 会等待进行中的恢复并释放其 Agent。历史事件保留在日志中，不会作为新通知重新发送。

持久化会话的并发请求共享完整的创建或恢复操作，包括已有会话的恢复分支。Bot 宿主负责表达归属、主角公开发言、刺激结算和发布资格；生活工具返回实际激活回执。

Bot 专用部署可使用 `qq_life/session/input`、`input_status`、`events` 和 `control`。有标识的输入在进入 inbox 前 fsync，按 `(session_id,input_id)` 串行化；ACK 丢失后核对 inbox 和已认领消息历史。会话世代不随传输重连改变。文字和编码图片复用现有附件接纳路径。事件分页保留原序号，由宿主提交连续游标并报告缺口。插话取消保留 inbox，当前步骤停止后重新推进其中首条待处理消息。适配器注册 `life_read`、`life_remember`、`life_intent`、`life_appraise`、`life_change_self`、`life_say`、`life_rest`；回调归属实际根 Agent 或其后代。只有宿主的范围化发送回调交付 QQ 正文。最终 say/rest 与其他工具混在同批时会在执行前被拒绝，`concludeTurn()` 不会取消同批其他调用。此部署扩展需要 Bot 专用 home 及实现反向生活回调的宿主，不会把独立 SDK 自动变成 Bot。

宿主为每个会话选择一份独立人格来源：主人私聊加载私有文件，其他会话加载公开文件。`life_read(kind=self,persona_audience=private|public)` 返回所选原文与文件 hash；宿主拒绝主人私聊之外的私有人格读取。`life_change_self` 的 `persona` 与 `persona_public` 目标分别修改对应文件，互不自动同步。Bot 部署中，`life_say` 默认发送后继续同一段活动，一次空闲输入可以触发多条消息和动作。角色用 `life_rest` 休息，或在最后一条表达显式设置 `end_turn: true`；等待答复的提问也会结束本轮。宿主确认表达并返回 `turn_complete: true` 后，适配器才结束表达轮；发送尚未确认不会结束。角色自行选择行动、告知或商量。可选的 `life_say.wait_for_reply` 提案要求宿主在发送问题前保存一个意图的等待；`life_intent` 也能保存这项等待。宿主阻止依赖此意图的行动，直到主角色理解了相应人物的新答复再恢复。告知、操作权限或时间流逝都不代表答复。无依赖的其他意图仍可继续。

Bot 握手声明 `relation_protocol: 2` 和 `resource_protocol: 1`。关系提案根据宿主读取的版本提交方向、程度和置信度；旧 delta 字段会被拒绝。持久关系材料通过明确的替换、解决和撤销操作更新。政策、来源核实及原子状态结算由宿主负责。

Bot 会话提供分页的 `qq_life/session/usage` 和限定范围的 `original` 结果读取。适配器分别记录每次实际模型流调用，包括压缩和失败尝试，仅保留最终累计用量样本。按宿主配置生成的文字预览复用本地 spill 存储；缺少可恢复存储时保留原结果。程序规范值、图片和音频保持原有语义。过期的 spill 引用明确报错。这些扩展只作用于绑定的 Bot 根会话及其实际后代。

`session/working-directory/get` 接收 `{ sessionId }`；`session/working-directory/set` 接收 `{ sessionId, path }`。两者均返回 `{ cwd }`，并可创建未知 Session，而不启动模型轮次。读取通过工作目录服务恢复缺失目录；修改保留起始目录元数据与权限，并通过已记录的用户上下文告知模型。

当运行时必须服务 SDK 客户端时挂载本插件：把它加入组合了 agent 服务的 `cordis.yml`，启动运行时，客户端即可通过 stdio 连接。常用路径是显式的——插件需要 `agents` 服务；其余每个能力都来自外围插件树。

Bot 适配器声明 `spending_protocol: 1`。每次有主的 `llm/stream` 尝试（包括子代理与压缩请求）之前，都会用 `kind: budget_reserve` 向可信宿主提交调用身份、提供方、模型、已接收输入引用和解析后的输出上限。只有 `ok: true` 才允许调用提供方；宿主不可达同样阻止请求。最终的 `budget_settle` 回调提供实际用量；结算失败或缺失时保留宿主的持久预留并记录错误。价格表、自然日、原子准入和预留恢复由宿主负责。不带预算回调的纯展示构造方式仍只统计用量、不执行准入。该接口不计量插件独立调用提供方 HTTP 接口或 Codex 等外部服务。

Bot 专属宿主可协商 `consult_protocol:1`，启用异步 Codex 交流工具 `life_consult`。宿主先校验范围、来源、准入和幂等，再返回冻结的执行参数。适配器通过有主的原生 `codex_consult` 作业启动桥接程序，先登记完成收集者、再授予启动许可；提交回执不阻塞主角色继续交流。取消仅作用于该作业，结算须等受管进程范围停稳。完整答复和重启恢复由宿主持久输入队列负责；受理作业或 worker 正常退出都不等于咨询答复已完整取得。部署须提供作业控制器与子进程服务，该能力默认不启用。

### 组装

插件在首次使用时为每个 `sessionId` 创建一个 agent。已注册的模型适配器优先用于该路由；尚无适配器负责的 `deepseek-official` 路由会挂载 DeepSeek 适配器，任何其他尚无适配器负责的提供方都会导致初始化失败。初始化成功前，所选适配器会解析确切模型与可选推理强度。

### 配置

| 字段 | 默认值 | 含义 |
|---|---|---|
| `maxTokensAsSuccess` | `false` | 把 max-token 轮次或 subagent 终止报告为成功的 SDK 结果 |

profile 组合拥有每个根 agent 的工具。`input`、`output` 与 `exit` 是仅供测试的运行时传输钩子；生产环境使用进程 stdio 与 `process.exit`。生成的[配置目录](../../../docs/config-catalog.zh.md#deepseek-aidsh-sdk-jsonrpc-server)是每个受支持字段的穷尽式真源。

### stdout 即协议

Stdout 只承载 JSON-RPC 帧，客户端可以逐字节解析；诊断信息应写入 stderr。请勿在组合的插件树中加入 stdout logger。

### SDK 客户端可以做什么

`initialize` 是运行时就绪边界：服务器由 Loader 组合挂载时，会等待当前插件树完成所有加载任务后再响应，因此首次提示词能够看到 MCP 初始工具发现等异步同级能力。握手返回协议稳定标识 `deepseek-harness-sdk-runtime`。服务器会通过所选适配器校验提供方／模型路由与可选的非空 `reasoningEffort`，再保存这些值；省略时不会保存推理强度，因此模型保留自身默认值。可选的正整数 `maxTokens` 会成为每个 SDK 创建的 agent 及其进程内后代的请求输出上限，省略时则应用所选适配器或提供方路由的默认值。JSON-RPC 请求可能并发分派，因此在一次 `initialize` 成功完成之前，`session/prompt` 会拒绝；客户端必须等待握手完成后再发送提示词。已接受的提示词会把一条带标识的用户消息排入队列，并立即返回 `{ messageId }`；服务器随后把每个持久事实作为 `session.event`、把整个 agent 生命周期的每次状态转换作为 `session.status` 流式发出。它不会把某条助手消息或 `turn/end` 归属于某个提示词，同一会话上的独立请求可以继续排入更多工作。持久化根目录与 persona 来自外围组合。

`session/wait` 观察已有 SDK 会话，直到受管理后代及其触发的根 Agent 后续轮次完成。它拒绝未知或已释放的根 Agent，不会创建会话，并在后代检查期间根 Agent 持续空闲后响应。收件箱消息处于停放状态的空闲子级继续驻留，但不延迟此响应；后续唤醒输入可使其继续运行。完成前发出的会话通知在 stdio 传输上先于响应到达。Agent 的运行时错误会保留在对应的确切会话中，直到后续提交的 `turn/end` 记录结果；否则等待请求会失败，即使错误发生在等待请求之前。终止事件已经记录的错误仍通过会话事件表达，后续成功的活动会清除较早的运行时错误。

### 关闭与退出

插件应答 `shutdown`，刷新响应并 dispose 根上下文，使 SDK 持有的 agent、订阅与持久化达到完全停稳，然后以 0 退出。EOF 与信号退出归 app bin 负责，后者也会 dispose 根上下文。仅卸载此插件会停止服务，但不会退出进程。

-----

<a id="understand-the-implementation"></a>
## 理解实现

协商 `compact_tool_protocol:1` 的 QQ life 会话使用 Python 主源的结构化 proposal schema 与六个基础工具；以 `ref`、`schema_hash` 和 `load:true` 精确读取能力目录后，才装入完整的当前定义。加载状态以仅作归属标记的 developer 消息保存，恢复后重新校验，并继续通过原宿主执行检查。完整版本化说明仍可精确读取和分页展开。未协商时沿用原有完整工具呈现。

Bot 请求装配在最终 header、动态事实与当前输入落盘后使用共享 token meter，执行普通聊天/任务的绝对软目标、版本化读取成对去重、持久图片退出，以及最多一次既有 DSH 交接。关键输入可超过软目标；已知物理容量不足时明确报错。未知容量与依赖适配器的估算保持明确标注。最近两批图片与有来源的 pin 保留；活动任务保守保留其图片引用。内部记忆会话沿用既有流程。

<details>
<summary>实现细节——点击展开</summary>

本节解释服务插件背后的设计；可观察行为已在[使用本包](#use-this-package)中完整说明。

### 设计理念

本插件是轻量的展示适配器：[`HarnessSdkJsonRpcServer`](src/server.ts) 负责协议方法与通知，传输与具名协议类型来自 `dsh-sdk-protocol`，与客户端 SDK 共享。它订阅会话、agent 与 subagent 生命周期事件，并把它们作为协议通知转发；只有当服务在生命周期建立快照时记录的 `local` 标志为 true 时才转发 subagent 完成事件——提供方名称、子级 id 与持久化谱系均不能证明本地性。

### 源码地图

| 文件 | 职责 |
|---|---|
| [`src/index.ts`](src/index.ts) | 插件入口：`Config` schema、stdio 接线、请求分发、共享关闭与退出任务 |
| [`src/server.ts`](src/server.ts) | `HarnessSdkJsonRpcServer`：协议方法、逐会话 agent 创建、生命周期订阅、清理 |

### 请求流程

每个协议方法在执行前都会校验输入并解析负责该请求的状态——`initialize` 保存 SDK 路由，`session/prompt` 解析存活的 agent 与会话配对并排入消息，`shutdown` 刷新响应，再 dispose 根上下文使其达到完全停稳，最后以 0 退出——共享退出任务确保竞争的 `shutdown` 请求绝不会重复 dispose 或退出。分发逻辑位于 [src/index.ts](src/index.ts) 与 [src/server.ts](src/server.ts)。

### 清理

`server.shutdown()` 关闭其精确根 Agent 下的后代准入，按子级优先顺序排空托管后代，再释放根句柄与适配器。它移除自身订阅，而仅卸载本插件时，无关父级树与外围上下文保持运行。后代排空失败会在其余根句柄与适配器的清理完成后报告。协议 `shutdown` 则 dispose 根 fiber，使持久化与整个运行时在进程退出前达到完全停稳。

</details>

-----

<a id="further-exploration"></a>
## 进一步探索

当插件约定不够用时阅读以下页面。它们从协议格式进入客户端与可运行应用。

- [SDK 协议格式](../protocol/README.zh.md) — 本插件服务的协议方法与载荷结构。
- [TypeScript SDK 客户端](../client/README.zh.md) — 驱动本插件的客户端。
- [SDK 应用组合包](../../bundle/sdk-app/README.zh.md) — 启动本插件的 `dsh --profile sdk` 应用。
- [Python SDK](../../../python/README.zh.md) — 驱动同一服务器的 Python 客户端。
- [SDK 运行时分发决策](../../../.agents/notes/implemented/architecture/2026-07-10-single-file-executable-sdk-runtime-distribution.zh.md) — 打包运行时为何服务封闭插件树。

-----

<a id="model-experience"></a>
## 模型体验

### SDK 用户消息

#### 模型看到什么

对于每个已接受的 `session/prompt`，文本和持久内容引用会原样进入一条用户消息。内联 `SdkEncodedImageBlock` 会先通过组合中的附件存储完成校验与提交，因此会话日志保留内容寻址的图片引用而不是 base64 字节。普通 SDK 会话使用外围组合的提示词与工具。绑定的 Bot 会话另有受众适用的稳定角色规则、完整动态快照和生活工具；规则与快照均有持久记录。协商 `capability_protocol:1` 和 `media_adapter_protocol:1` 后，才展示相应的新增工具参数。Bot 目录只读取已存活且世代匹配的 Agent；本机方法 provider 注册到该 Agent 的作用域，加载正文时复核 hash，销毁时撤销注册。工具政策身份来自实际注册对象，不来自模型参数。 `memory_protocol:1` 握手支持同一角色的内部记忆工作，由宿主上下文的 `processing_mode: internal` 开启。该 Agent 只展示 `life_read`、`life_remember` 和 `life_rest`；执行检查也会阻止本地作用域中注册的其他工具。模式在该 Agent 内保持固定，切换模式需要新建 Session。摘要成功回应带有 `turn_complete: true` 时，直接结束本轮，不再发起一次模型请求。

#### Token 影响

依数据而定的用户消息 token 会进入保留的会话历史，并在后续轮次中重复发送，直至另一个包将其压缩（compaction）。方法短描述使用原生 skill 目录，正文通过 `skill` 按需加载；目录不变时不重复注入。JSON-RPC 帧、会话通知与服务器内部记录不会增加模型上下文 token。

#### KV Cache 影响

普通提示词输入追加在可复用请求前缀之后。Bot 动态快照作为用户上下文更新写入日志；受众或规则版本变化可以替换稳定角色规则区。实际缓存收益由提供方报告的命中用量确认，不能仅凭 section 接口推断。

## 已知限制与延期工作

<a id="known-limitations-and-deferred-work"></a>


这些限制说明本插件何时需要特别的运维注意。它们是当前包约束，不是与其他服务方式的对比或任务积压。

- **协议没有逐会话关闭或提示词取消方法**——SDK 创建的 agent 会一直存活到进程关闭。
- **没有逐提示词结果**——`MessageId` 只标识 inbox 准入；拥有自动化活动区间的客户端必须自行定义并观察该区间。
- **stdout 纯净性由部署保证**——外围配置仍可能加载 stdout logger 并破坏 JSON-RPC 通道；此插件不会检查或否决同级 logger。
- **自动挂载适配器仅支持 DeepSeek**——`initialize` 可以复用任何预先注册的模型适配器，但唯一的回退行为是挂载 DeepSeek 适配器。

<a id="dev-note"></a>
### 开发备注

<details>
<summary>维护者的工作上下文——点击展开</summary>

本开发备注是维护者的工作上下文，明确不具权威性——已交付的行为与限制见上文各节与代码。单文件可执行运行时分发将本插件与打包的 `jsonrpc-demo` bin 配对；请让关闭与退出约定与负责 EOF 和信号退出的 app bin 保持一致。没有记录其他未解决的开放设计问题。

</details>
