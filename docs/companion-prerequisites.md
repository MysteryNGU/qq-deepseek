# P1 实施前置核验

日期：2026-11-17。性质：实施前置报告，含实测证据、契约声明与待决项；**本文不含任何生产代码或数据修改**。
输入：`E:\wsl\2026-11-17-companion-awareness-subagents-design.md`（下称“设计”）、`E:\wsl\2026-11-17-conversation-audit.md`（下称“审计”）、`docs/companion-awareness-baseline.md`（下称“基线”）。
证据口径：每条结论都注明来源。实测证据写命令与返回值；源码证据写文件与行号；拿不到的写 `unknown`，不用推测填充。

本轮环境：
- 云端 `47.104.8.84`（Ubuntu 24.04.2，Node `v22.19.0` @ `/opt/qq-deepseek/toolchain/node/bin/node`，系统 node 为 `v18.19.1` 不可用于 DSH）。
- 生产服务 `companion.service`：`active`、`NRestarts=0`、`ActiveEnterTimestamp=Tue 2026-11-17 00:55:03 CST`；本轮结束时复核仍为 `active`，`08:20` 之后日志 `Traceback|ERROR|CRITICAL` 计数为 `0`。
- 生产数据未被读写：`life.db` 只经 `file:...?mode=ro` 只读句柄查询过两张会计表；探针一律使用一次性 `TemporaryDirectory`。

---

## 1. DSH SDK 真实能力核验

### 1.1 版本与入口

| 项 | 实况 | 证据 |
| --- | --- | --- |
| DSH CLI | `0.2.1-rc.2` | 云端 `node vendor/dsh-runtime/lib/bin.js --version` → `0.2.1-rc.2` |
| 本地 monorepo | `0.2.1-rc.2` | `vendor/dsh-bridge-work/package.json` |
| Python SDK | 6 文件，两端字节一致 | 基线 §3.1 sha256 对照 |
| 云端启动入口 | `vendor/dsh-runtime/lib/bin.js` 是软链，目标在 `toolchain/dsh/lib/node_modules/@deepseek-ai/dsh/lib/bin.js` | `ls -la vendor/dsh-runtime/lib` |
| 本地启动入口 | 无 `lib/bin.js`；走 `apps/cli/src/bin.ts` + `node_modules/tsx` | `Test-Path` 两处均 `True` |
| 生产使用的 profile | `sdk-minimal` | 云端 `life/companion_dsh_worker.py` 固定 `profile='sdk-minimal'` |

### 1.2 实测探针与结果

新增可复跑探针 `tools/verify-dsh-sdk-capabilities.py`（只读生产、只写临时目录、只调模型接口）。云端执行结果：

```text
[ok] dsh.version        {"value": "0.2.1-rc.2"}
[ok] message.turn       {"finish_reason": "completed", "said": "在的"}
[ok] session.resume     {"sessionId": "probe-resume-1", "finish_reason": "completed", "said": "在的"}
[ok] session.resume.missing {"rejects": true, "error": "JsonRpcError: session \"probe-does-not-exist\" not found"}
[ok] rpc.surface        {"qq_life/hello": "unknown ... SDK runtime method", "qq_life/tools/list": "unknown ...", "session/wait": "unknown SDK session: probe-tools"}
[ok] tools.execute      {"finish_reason": "completed", "said": "7c1ea9"}
[ok] tools.inventory    {"said": "bash, read_image, working_directory"}
[ok] subagent           {"said": "没有子代理"}
[ok] home.written       {"count": 13, 均为 profiles/ 与 sessions/ 文件}
cleaned=True  EXIT 0
```

关键点：`session.resume` 是在**新进程**里恢复上一进程写下的会话，随后同一 `sessionId` 继续对话并拿到正确回答（“在的”），证明恢复的是真实持久会话，不是同进程内存。

### 1.3 实测踩到的坑（不写下来就会被重复踩）

1. **`DEEPSEEK_BASE_URL` 必须是 `https://api.deepseek.com/anthropic`。** 用根地址 `https://api.deepseek.com` 时每次 `turn/end` 都是 `{"kind":"error","error":{"message":"Not Found (request_id: …)","code":"HTTP_404","status":404}}`，而错误文案不含任何 URL 线索（`request_id` 也搜不到），第一次会误判成“SDK 用不了”。生产侧 `life/companion_executor.py` 在 `kind=='dsh'` 分支已做同样的改写，`life/services/dsh_bridge.py::_env()` 也做；**任何新写的 SDK 调用点必须照抄这一条**。
2. **HTTP 200 不等于有效回复。** `sdk-minimal` 的 JSON-RPC server 配置为 `maxTokensAsSuccess: false`，截断会以 `finish_reason≠completed` 或 `turn/end.kind≠completed` 呈现，验收要读 `turn/end.reason.kind`，不能只看 RPC 是否返回。
3. **Python SDK 的 `HarnessClient.request()` 强制 `response_model`。** 自定义通道（`qq_life/*`）没有自带模型时，需传一个 `extra='allow'` 的透传模型；否则是 `TypeError` 而不是协议错误。
4. **模型名以生产配置为准：`deepseek-flash`。** 文档里的 `deepseek-v4-flash` 在本部署返回 404。
5. **`session.wait` 在 `sdk-minimal` 上是“SDK 会认的方法、但要有已存在的会话”**：返回 `unknown SDK session`，不是 `unknown method`。

### 1.4 逐项判定

| 设计所需能力 | 判定 | 依据 |
| --- | --- | --- |
| 会话恢复（跨进程、持久） | **supported** | 1.2 `session.resume`；`session/resume` 在 `server.ts:687-693`；缺会话时明确拒绝而不是新建（1.2） |
| 消息投递与幂等标识 | **supported** | `session/prompt` 返回 `messageId`；回执事件 `agent/inbox/spliced`（`python/sdk/src/deepseek_harness/api.py:211-221`） |
| 工具调用 | **supported** | 1.2 `tools.execute`：模型真实读取了临时目录里的文件并复述 token |
| 工具清单（模型视角） | **supported** | 1.2 `tools.inventory` → `bash, read_image, working_directory`；与 `--dump-config` 的 `tool-fs{read:false,write:false,edit:false}`、`tool-working-directory`、`terminal-bash` 一致 |
| 事件流 | **supported** | `session.event` / `session.status` 订阅；`turn/end` 携带 `reason.kind` |
| 子代理（模型可调用） | **unsupported（在 `sdk-minimal` profile 内）** | 1.2 `subagent` → “没有子代理”；`--dump-config` 的 35 行里没有 `subagent` / `tool-subagent` / `tool-subagent-control` |
| 子代理（DSH 本体能力） | **supported，但需要新 profile** | 本地 monorepo `packages/subagent/*`（`subagent`、`subagent-spawn-in-process`、`subagent-fork-in-process`、`tool-subagent`、`tool-subagent-control`）、`packages/sdk/protocol/src/types.ts:136-142` 的 `subagent.started` / `subagent.finished` 通知；基线 §4.4 |
| 逐次用量 / 计费 | **unsupported** | `RunResult` 无 usage；RPC 面无 usage；`life/companion_dsh_worker.py` 明确写 `usage=None` |
| 自定义通道 `qq_life/*` | **unsupported（在 `sdk-minimal`）** | 1.2 `rpc.surface`：`unknown DeepSeek Harness SDK runtime method`；对应实现在 `packages/sdk/server/src/server.ts:671-725`，随 `@deepseek-ai/dsh-sdk-app` 提供 |

**结论**：设计 §6.3 要求的“先检查真实支持”得到明确答案——会话、消息、工具、事件可用；**子 agent 工具、用量计量、`qq_life/*` 通道在云端当前使用的 `sdk-minimal` profile 里都不存在**。这三项不能靠提示词补，必须由 B 阶段决定走哪条路（§2.4、§4）。

---

## 2. 预算三模式

### 2.1 现状

- 云端 `config.toml`：`"spending_policy" = {"enabled": false, "daily_target_fen": 2000, "daily_limit_fen": 5000}`。即当前 DSH 通路没有被预算拒绝，但一旦 `enabled=true` 就会被拒。
- 预算阻断的三处（基线 §2.1-2.3）：`life/companion.py:1327` 从 `available_actions` 去掉 `dsh`；`life/companion.py:2661` 执行处 `raise SessionFailure('budget_unmetered', …)`；`life/companion.py:2764` 记账豁免。
- 现有预算模型（`life/spending.py`）只有一个开关，没有模式：`SpendingPolicy(enabled, daily_target_fen, daily_limit_fen)`，未知用量一律**保留预留**计入 `reserved_nano` 与 `unknown_calls`（`spending.py:82-104`、`:192-196`），从不写 0——这一点已经符合设计 §11“未知用量不写成 0”，只是没有对外暴露成模式。

### 2.2 三模式定义（本文为准，实现按此做）

| 模式 | 准入条件 | 允许缺失的部分 | 必须记录的 | 必须展示的限制 |
| --- | --- | --- | --- | --- |
| `strict` | 本次调用的完整计费已知（provider×model 有价格、`inputTokens/cacheReadTokens/outputTokens` 齐全、`cacheWriteTokens` 为 0 或缺失），且预留额可算出 | 不允许 | `charged_nano`（精确）、`price_version`；缺任一项即拒绝并给出 `budget_unpriced` / `budget_unbounded` | 无 |
| `bounded_estimate`（**建议默认**） | 只要可实现的上限存在：并发上限、单任务运行时长、调用次数、单次 `max_tokens` 与总预留上限 | 允许 usage 缺失、允许跨重试/压缩的用量不完整 | 已知部分计入 `charged_nano`，未知部分**明确计入 `unknown_calls` 与 `reserved_nano`**，并写 `estimate_sources`（价格版本、上限值） | 明确写出“这是上限估算，不是账单”；并发/时长/次数中任一维度若 SDK 不能强制，必须标 `unsupported` 并只落实可落实的那几维 |
| `off` | 不设准入，不预留 | — | 只记账不阻断；未知用量仍记 `unknown`，不写 0 | 明确写“当前无预算保护” |

### 2.3 迁移与默认值规矩

1. **不静默改变已有的 `strict`。** 现有配置只有 `enabled` 布尔。迁移规则：`enabled=true` 且没有 `mode` 键 → 视为用户已明确要求“完整计费才可执行”，**保持 `strict`**，不得因为产品默认值是 `bounded_estimate` 就把它悄悄改成别的模式。
2. `enabled=false` → `off`。
3. 新装/未配置 → `mode=bounded_estimate`（产品默认）。
4. 同时保留 `enabled` 字段一段时间：`enabled=false` 等价 `mode=off`；`enabled=true` 且 `mode` 存在时以 `mode` 为准。冲突时（`enabled=false` 但 `mode=strict`）启动即报配置错误，不做猜测。
5. 任何模式切换都写一条审计（时间、旧值、新值、来源），并让小鱼在下一次认知里读到“现在是哪个模式、哪一维做不到”。

### 2.4 各维度能不能真落实（实测结论）

| 限制维度 | 能否由 SDK 强制 | 证据 |
| --- | --- | --- |
| 并发上限 | **能（在应用层）** | 一个 `DeepSeekHarness` 进程内可多会话，但并发需由宿主用信号量限制；SDK 不提供全局配额 → 应用层实现 |
| 单任务运行时长 | **能** | `session/prompt` 之后宿主可 `cancel`；`qq_life/cancel` 已有 `waitMs` 与 `settled` 语义（`server.ts:623-647`），但那是 `qq_life` 通道；`sdk-minimal` 下只能用进程树终止 + 超时 |
| 调用次数上限 | **不能由 SDK 强制** | 无任何调用计数 RPC；只能靠宿主对“每轮允许的步数”做外部观测（事件里能看到每次 `assistant/message`/工具调用） |
| 单次输出上限 | **能** | `DeepSeekHarnessConfig.max_tokens` 已生效；`--dump-config` 的 `sdk-jsonrpc-server` 有 `maxTokensAsSuccess: false` |
| 费用估算 | **只能估算** | 无 usage RPC（§1.4）→ `bounded_estimate` 必须把 `max_tokens × 单价 + 上下文上界` 作为上界，无法给出真实账单 |

→ 结论：`strict` 对 DSH 结构性不可行（SDK 不提供完整计费），**`bounded_estimate` 是唯一可行方向**，但“调用次数上限”必须如实标 `unsupported`，只落实并发、时长、token 三维，并在展示里写清“估算不是账单”。

---

## 3. 隔离 workroot 与工具产出路径

### 3.1 事实

| 事实 | 证据 |
| --- | --- |
| 云端根 `/opt/qq-deepseek` 属主 `root:root`（`755`），`life/` 与 `work/` 属主 `companion:companion` | `stat -c '%A %U:%G %n'` |
| 服务以 `User=companion` 运行，`WorkingDirectory=/opt/qq-deepseek` | `systemctl cat companion.service` |
| `life/companion_dsh_worker.py` 把 `config.cwd` 直接设为 `request['cwd']`，宿主未给时取 `self.home` | `life/companion_executor.py` 的 `cwd = Path(action.get('cwd') or self.home)` |
| DSH 桥以 `danger-full-access` 起 worker（`life/services/dsh_bridge.py::_env`），`sdk-minimal` 文档同样写明“不把访问限制在 cwd 里” | `docs/user/guide/python-sdk.md` §“Understand the minimal profile” |
| SDK 会在 harness home 写 `profiles/`、`sessions/`、`.anonymous-user-id` | 探针 `home.written` 13 项 |
| 云端 `work/` 为空目录，属主 `companion` | `ls -la /opt/qq-deepseek/work` |

**风险**：`danger-full-access` 下“工具产出不落 cwd、不落生产 HOME”**不可能靠 cwd 参数实现**。cwd 只影响相对路径解析，不限制写到哪里；模型一句 `cd /var/lib/companion/xiaoyu` 就出去了。

### 3.2 规矩（实现必须遵守）

1. **每个执行任务一个隔离 workroot**：`<home>/workroot/<task_ref>/`，或者云端专用 `/opt/qq-deepseek/work/<task_ref>/`（`companion` 可写）。任务开始时创建，结束时保留产物、清理临时文件。
2. **不把生产 HOME 当工作目录**：`<home>` = `/var/lib/companion/xiaoyu`，只能作为配置与数据的根，**永远不作为执行 cwd**。`companion_executor.py` 当前的 `cwd = action.get('cwd') or self.home` 必须改为“没有显式 workroot 就拒绝执行”，而不是回落到 home。
3. **DSH harness home 与工作目录分离**：`dsh_home` 放 `<home>/data/dsh-linux`（现状），`cwd` 必须是 workroot；两者不能是同一个目录。
4. **禁止写入清单**：`<home>/data/life.db*`、`<home>/config.toml`、`<home>/persona.md`、`<home>/data/companion/`、`<home>/data/notes/`、`/opt/qq-deepseek/data/`、`/root/.android/`、`/etc/systemd/system/`。
5. **落盘位置**：工具大输出、日志、构建产物写到 workroot 下的 `artifacts/`；需要长期保留的复制进 `<home>/data/companion/artifacts/`（现有目录），不经由 cwd。
6. **TMP/TEMP**：DSH 子进程的临时目录必须指向 workroot 内的 `tmp/`，不能继承系统 `/tmp`（现状 `life/services/dsh_bridge.py::_env` 用 `cfg.dsh_tmp`，云端为 `data/tmp`，属另一处要改的回落）。
7. **验收方法**：每个执行任务结束后检查 workroot 之外的文件 mtime 未变（`find <home>/data -newer <起点> -not -path '<workroot>/*'`），以及 `/opt/qq-deepseek/life` 未被修改。

### 3.3 现状判定

- 现状**不满足**“工具产出不落 cwd、不落生产 HOME”：没有 workroot 概念，cwd 缺省回落到 home，权限是 `danger-full-access`。
- 本轮不做修改，只把规矩与验收方法固定下来，交给 B 阶段的执行层实现。

---

## 4. 云端补丁要求

三条都是“必须修”的硬要求，本报告给出问题、证据与验收标准，不在本轮改代码。

### 4.1 隔离数据库

- 现状：生产 `life.db` 单文件，含 `-wal`/`-shm`（约 328 MiB）。测试若要“只读副本”，必须用 SQLite 只读句柄复制（审计报告已用 `mode=ro`），且复制品放在临时目录。
- 要求：任何测试/评估进程**只能**打开临时目录里的副本或全新临时库；连接串必须显式 `mode=ro`（只读场景）或指向临时路径。生产库路径不得出现在测试配置文件里。
- 验收：跑完测试后 `life.db` 与 `-wal` 的 `mtime` 不变；`spending_calls`、`usage_calls` 中无探针产生的行（本轮已按此核对：`usage_calls` 在探针窗口内为 0 行，见 §7.3）。

### 4.2 客户端接口不得访问主人私聊库

- 设计 §5.5 第 2 段：小鱼本人可检索自身跨会话经历，但“其他聊天参与者不能通过客户端 API 拉取主人私聊数据库”。
- 现状实测（通用工具边界 `life/tool_boundary.py`，本地副本与云端同构，37 行正则 `_DATABASE`）：

| 用例 | 判定 |
| --- | --- |
| `sqlite3 life.db 'select 1'` | 拒绝 |
| `cat /var/lib/companion/xiaoyu/data/life.db` | 拒绝 |
| `open('…/life.db','rb').read()` | 拒绝 |
| `open('life.db','rb')` | 拒绝 |
| `ls -la data/*.db` | **放行** |
| `open('life'+'.db','rb')` | **放行** |
| `glob.glob('**/*.db')` / `Path('.').rglob('*.db')` | **放行** |
| `bash -c true`（不含库名） | 放行（合规） |

- 结论：`tool_boundary.check_tool_arguments` 是**字符串正则**，能挡住“把库名写进参数”的直接访问，**挡不住**通配、拼接、遍历后再读。它是一条“应用层工具边界”，源码注释自己写明“不是 OS sandbox”。
- 要求（按优先级）：
  1. **客户端侧**：`companion_remote` 暴露的 `/rpc`、`/mobile/`、`/companion/*` 只返回小龙自己的投影与已发出的内容；不存在“按 conversation 拉主人私聊原文”的接口。带 `Authorization: Bearer <home>/data/companion/server-token` 的 owner 通道可以读本人历史，第三方会话身份不行。
  2. **执行侧**：workroot 内运行的子 agent 不得持有 `<home>/data` 的可读路径；在 Linux 上用 mount namespace 或文件权限把 `life.db` 从执行上下文里拿掉，而不是继续加正则。
  3. **工具边界**：`tool_boundary.py` 至少补上通配/遍历/拼接的检测，并把 `*.db` 之外的 `life.db-wal`、`companion/` 目录一并覆盖。
- 验收：构造上表全部用例 + 三个“遍历后读取”的用例，要求**逐条拒绝**；并证明拒绝发生在参数检查阶段（不执行）。

### 4.3 会话描述脱敏

- 现状：DSH 会话日志写在 `<home>/data/dsh-linux/sessions/…/session.v4.jsonl`，其中包含完整 prompt、工具调用与结果。基线 §2.5 已确认云端存在 3 个真实会话目录。
- 要求：
  1. 会话 `title` / 列表摘要不得包含私聊原文、文件名或个人数据；只允许“任务类型 + 目标对象 + 时间”这类结构性描述。
  2. 对外暴露（`/companion/artifacts`、日志页、诊断输出）的会话片段必须脱敏：去掉消息正文，只留 `conversation_ref`、计数与时间。
  3. 子 agent 的普通日志不得包含主人私聊内容或二维码等私有媒体（设计 §7.3 也要求二维码不进子 agent 日志）。
  4. 会话清理遵循既有 `purge_sessions` 的归属校验（`life/services/dsh_bridge.py::purge_sessions`），不得删除非 bot 归属的会话。

---

## 5. 文档引用缺口

### 5.1 设计 §12.1 点名的 5 份文档：本地与云端都不存在

| 文档 | 本地 | 云端 |
| --- | --- | --- |
| `docs/companion-qq-design.md` | 缺失 | 缺失 |
| `docs/companion-message-routing.md` | 缺失 | 缺失 |
| `docs/companion-continuity.md` | 缺失 | 缺失 |
| `docs/companion-followthrough.md` | 缺失 | 缺失 |
| `docs/companion-autonomous-development-handoff.md` | 缺失 | 缺失 |

云端 `docs/` 实有 4 份（`companion-bilibili.md`、`companion-bilibili-login.md`、`companion-bilibili-social.md`、`companion-cloud-android-build.md`），本地 `docs/` 有 17 份，两者交集为 0。
→ 后果：设计要求的“先读交接文档”前提不成立。**替代材料**：`docs/companion-awareness-baseline.md`（云端结构、QQ 接收面、Android 构建链、验收覆盖矩阵）+ 本文。B 阶段开始前应把这两份作为事实来源，而不是等这 5 份凭空出现。

### 5.2 代码里引用但部署上找不到的文档/路径

| 引用处 | 引用的路径 | 该路径是否存在 |
| --- | --- | --- |
| 云端 `life/companion_context.py:34`（`computer` 工具说明） | `skills/computer.md` | 云端**不在** `/opt/qq-deepseek/skills/`；**在** `/var/lib/companion/xiaoyu/skills/computer.md`（1783 B，`0600`）|
| 云端 `life/companion_computer.py:82` | `self.home/'skills/computer.md'` | 同上，代码是对的 |
| 云端 `life/companion_life_material.py:3`（注释） | `docs/companion-next-agent-handoff.md §3` | 云端与本地都**不存在** |
| 本地 `config.toml` / `config.example.toml` | `tools/node-v22.19.0-win-x64/node.exe`、`tools/tmp` | 本地与云端都**不存在**（本地被 `.gitignore` 之外的路径引用，需按机器改写） |
| 本地 `migration-agent-handoff.md` | `tools/platform-tools/adb.exe` | 本地与云端都**不存在** |

补充：`docs/xiaoyu-work-mode-design.md:31` 在 2026-11-16 就记录过 `companion-next-agent-handoff.md` 与 `skills/computer.md` 缺失，本轮核实结果是“后者已存在、前者仍缺失”——说明这类缺口必须在同步时核对，不能靠记忆。
→ 规矩（写进 `docs/cloud-sync-policy.md` §3）：文档参考先按“代码真实使用的路径”核对，再写进文档；引用不存在文件时标注 `unknown`。

---

## 6. 2026-11-17 对话审计两个缺陷的复检

方法：对云端生产代码（`/opt/qq-deepseek/life/companion.py`，`2829` 行）做静态核对。本地 HEAD（`e953315c`）**没有** `life/companion.py`，修复代码只在云端，因此两条结论都以云端源码为准；本地无法复跑对应集成用例。

### 6.1 缺陷一：解析失败整轮失声

审计修复（审计文档“修复与验证”节）：`_decode_decision` 抛 `JSONDecodeError` 时新增 `_salvage_reply`，把正文当纯回复交付，并记一条审计；正文不可取时对话轮回一句诚实的“没接上”。

复检证据：云端 `life/companion.py:1704` 存在 `def _salvage_reply(self, result, purpose, source, error)`；`life/companion.py:1740` 的 `_decode_decision` 是被调用的解码入口；`grep -c '_salvage_reply\|_same_intent'` 命中 6 处，说明修复点与调用点都在。

复检结论：**已修复（对话轮）**。仍然存在的边界：
- **行动轮解析失败仍然失败**，不猜动作——这是刻意保留的安全边界，不是缺陷。
- “修复代码在云端且已部署”有源码与部署记录支持，但**没有**在隔离环境重跑审计用例 3（`evt_7a84c9442c39427e` 那一轮的失败路径需要强制 `JSONDecodeError`，需要云端夹具）。该条标 **needs_recheck**。
- 建议补一条回归（在云端隔离环境做）：强制 `_decode_decision` 抛 `JSONDecodeError`，断言 (a) 对话轮有回复事件且 `turn_id` = 输入事件 id；(b) 审计有“已把原文当回复交付，未执行任何动作”；(c) 行动轮仍然失败且不执行任何动作。

### 6.2 缺陷二：措辞改写重复建项

审计修复：新增 `_same_intent`——共享同一文件名/网址（≥4 字符 ASCII 词）直接判同；否则按实词重叠（严格 0.5/4 词，同轮放宽到 0.3/4 词且至少 2 个非通用实词）。用于“项目更新换 key”和“承诺换说法”两处。

复检证据：云端 `life/companion.py:2216` 存在 `def _same_intent(cls, left, right, *, strict=True)`，与审计描述的“严格 0.5/4 词、同轮放宽到 0.3/4 词”签名一致。

复检结论：**已修复，且修复是启发式，不是语义模型**。仍然存在的边界：
- 极端改写（既无共享文件名/网址，实词也几乎不重叠）仍可能漏合并；漏了只多一份项目，不会吞回复。
- 跨轮的两份项目如果共享同一个文件名仍会被合并——审计已覆盖该用例，但这意味着**同名的两次独立工作会并成一份**，需要在 B 阶段的任务模型里用 `idempotency_key` 区分，而不是继续加深启发式。
- 与本次设计的关系：设计 §6.1 的 `create` 要求 `idempotency_key`，正是把这个启发式换成显式去重。B 阶段必须优先用显式 key，`_same_intent` 只作为兜底。

### 6.3 复检的诚实边界

- 两条结论基于云端源码静态核对 + 审计文档记录的修复说明，**没有在云端隔离环境重跑完整审计用例**（重跑需要强制失败注入，属另一项工作）。
- 因此本文对这两条的最终状态是：**缺陷一“对话轮已修复、行动轮按设计仍失败”，缺陷二“已修复为启发式合并、极端改写仍可能漏”**，两条都未发现需要推翻审计结论的新证据；也都未取得“部署后从未复发”的独立证据，标 `needs_recheck`。
- 云端 `life/companion.py` 没有被纳入本地 Git（本地没有这个文件），这是 §5.2 回写规矩最紧的一处：在它被回写并提交之前，任何“修改认知循环”的本地改动都无法与线上对齐。

---

## 7. 给 A / B / D 的接口契约

以下契约是后续阶段的实施边界。字段名可在实现时调整，但**语义与失败要求不得放宽**。

### 7.1 A（认知层）依赖的两个读取接口

**能力目录**（设计 §5.4）：

```text
capability_ref : str          稳定标识，如 "agent.create"、"dsh.session.resume"
category       : enum         model | tool | agent | channel | node | media | runtime
description    : str          给小鱼看的一句话，不写实现术语
node           : str          运行节点：cloud-linux | owner-windows | owner-android
status         : enum         ready | needs_setup | offline | unsupported | unknown
reason         : str          status≠ready 时必填，写清楚缺什么（账号、权限、依赖、模型离线）
checked_at     : iso8601      最近一次真实探测时间
schema_ref     : str | null   详情读取入口
```

约束：
- 没有真实可调用路径的能力**不得**标 `ready`。本轮实测的初始值：`dsh.session.resume=ready`、`dsh.tools=ready`、`dsh.events=ready`、`dsh.subagent=needs_setup`（`sdk-minimal` 无委派工具）、`dsh.usage=unsupported`、`qq_life.channel=unsupported`（`sdk-minimal` 无该通道）。
- `offline` 必须有 `checked_at` 与探测方式；`unknown` 不得被渲染成“不会做”。

**按需认知读取**（设计 §5.5）：

```text
kind          : awareness | people | conversations | experience | attention | capabilities | agent_tasks
query/ref/cursor/limit/since/until/actor_ref/conversation_ref : 适用过滤字段
→ items, next_cursor, source_refs, freshness, truncation
```

约束：读取不产生对外发言、不默认改写记忆。跨会话内部回忆对小鱼本人开放；客户端对外接口不得借此拉取主人私聊原文（§4.2）。

### 7.2 B（子 agent）依赖的七个操作与 DSH 的缺口

设计 §6.1 的七操作（`kind=agent, op=create|send|inspect|wait|cancel|resume|read_artifact`）与本轮实测的对应关系：

| 操作 | DSH 侧可用基础 | 本阶段判定 |
| --- | --- | --- |
| create | `session/prompt` 开新 `sessionId`（§1.2） | **可做**（用新会话模拟执行节点） |
| send | `session/prompt` + `messageId` 幂等 | **可做** |
| inspect | `session.event` 订阅 + 自建状态表 | **可做**，状态由宿主维护 |
| wait | 订阅 `session.status=idle`；`session/wait` 要求会话已存在 | **可做**，需短时限与游标 |
| cancel | 仅能终止子进程树（`sdk-minimal` 无 `qq_life/cancel`） | **部分**：能停，不能像 `qq_life` 那样给出 `settled` 语义 |
| resume | `session/resume`（§1.2 实测） | **可做**，且必须区分“真实恢复”与“从检查点新起 attempt” |
| read_artifact | 宿主读 workroot 产物 | **可做** |

必须补齐的宿主能力（SDK 不提供）：父子关系（SDK 只在 `subagent.started/finished` 里有 `parentSessionId`/`childSessionId`，且需要子代理被真实启用）、检查点、并发配额（合计 3、嵌套 2）、用量与预算模式（§2）、节点租约。

**明确不允许**：因为 `sdk-minimal` 没有委派工具，就把“子 agent 可用”写进口头承诺；B 阶段要么启用带 `tool-subagent` 的 profile 并实测，要么把 `dsh.subagent` 保持 `needs_setup` 并用宿主侧执行节点实现七操作。

### 7.3 D（发布）依赖的事实维度

```text
release_ref : str            绑定源文件 manifest / commit / 平台 / 版本
built / verified / published / download_verified / device_verified : bool + evidence_ref
sha256 / size / signature_id / build_node / rollback_pointer
```

约束：五个维度分别记录，不能用一个 `completed` 掩盖真机未测；上传后必须用客户端方式实际下载并核对哈希；发布动作需用户确认（见 `docs/cloud-sync-policy.md` §6）。

### 7.4 所有阶段共用的预算接口

```text
mode           : strict | bounded_estimate | off        （默认 bounded_estimate）
limits         : {concurrency, task_seconds, calls_per_task, max_output_tokens, daily_limit_fen, daily_target_fen}
enforceable    : {concurrency: bool, task_seconds: bool, calls_per_task: bool}
accounting     : {charged_nano, reserved_nano, unknown_calls, price_version, estimate_sources}
```

`enforceable.calls_per_task` 在本轮实测为 `false`（§2.4），必须在对小鱼的展示里如实出现。

---

## 8. 待决项与风险

1. **云端生产代码与本地不同源**（云端 47 个 `companion_*.py`、`role_agent.py` 差 749 B）。B 阶段动手前必须按 `docs/cloud-sync-policy.md` §5.2 回写本地并提交，否则所有“本地测过”都是空话。
2. **子 agent 路线未定**：`sdk-minimal` 无委派工具；启用带 `tool-subagent` 的 profile 会改变生产执行形态（新 profile 需要 `pnpm` 与插件安装），需与用户确认后再做。
3. **`danger-full-access` 与“工具产出不落生产 HOME”直接冲突**（§3）。若不改权限模型，只能靠“每个任务隔离 workroot + 事后核验”，不能声称已隔离。
4. **`tool_boundary` 不是沙箱**（§4.2）。客户端接口与执行侧权限才是真正的边界。
5. **审计两缺陷的云端复现未做**（§6.3），标 `needs_recheck`。
6. **`napcat.service` 当前 `inactive`/`disabled`**，QQ 链路未启用（基线 §5.1）；C 阶段任何 QQ 相关验收都必须先得到用户对“启用 NapCat”的明确同意。
7. 本地 `python` 环境缺 `pydantic`（`python -c "import pydantic"` → `ModuleNotFoundError`，`.venv` 下无 `pydantic`/`openai`/`httpx`），`tools/verify-dsh-sdk-capabilities.py` 目前只能在云端 venv 或具备 `pydantic` 的环境跑；本地跑前需先确认解释器。

---

## 10. 可复跑验证

```bash
# 云端（只读生产、只写临时目录）
export PROBE_KEY=$(/opt/qq-deepseek/.venv-companion/bin/python -c \
  "import tomllib;print(tomllib.load(open('/var/lib/companion/xiaoyu/config.toml','rb')).get('api_key',''))")
/opt/qq-deepseek/.venv-companion/bin/python /opt/qq-deepseek/tools/verify-dsh-sdk-capabilities.py

# 只检查参数边界（不启动 SDK，不需要凭据）
python - <<'PY'
import sys; sys.path.insert(0, '.')
from life.tool_boundary import check_tool_arguments
for tool, args in [('stdio', {'command': 'cat life.db'}),
                   ('stdio', {'command': "cat life'+'.db"}),
                   ('stdio', {'command': "ls -la data/*.db"})]:
    try: check_tool_arguments(tool, args); print('ALLOWED', args)
    except ValueError: print('BLOCKED', args)
PY
```

---

## 9. 本轮实际执行与清理

- 云端：临时目录 `xiaoyu-sdk-probe-*` 全部删除（`cleaned=True` × 6 次），`/tmp` 下探针脚本（`sdk_probe*.py`、`read_key.py`、`probe_check*.py`、`permcheck.sh`、`healthcheck.sh`、`run_probe.py`、`verify-dsh-sdk-capabilities.py`、`dsh-sdk-report.json`）全部删除，复核无残留。
- 生产：未写 `life.db`；探针窗口内 `usage_calls` 新增 0 行；`companion.service` 全程 `active`、`NRestarts=0`、新日志 0 条 `Traceback|ERROR`。
- 未向 QQ、群、外部平台、发布通道发起任何动作；未重启服务；未修改生产代码与配置。
- 新增文件：`tools/verify-dsh-sdk-capabilities.py`（可复跑探针）、`docs/cloud-sync-policy.md`、本文。
