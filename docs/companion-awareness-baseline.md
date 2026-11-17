# 小鱼持续认知与子 agent：P0 基线与能力侦察

日期：2026-11-17。性质：只读侦察报告，不包含任何生产代码或数据修改。
输入：`E:\wsl\2026-11-17-companion-awareness-subagents-design.md`（下称“设计”）§3、§6、§7、§11、§12。
证据约定：凡标 `[证据]` 的行号/命令输出均来自本次实际读取；无法取得证据的写 `unknown`，不做推测填充。

侦察环境：
- 云端 47.104.8.84，`uname -a` → `Linux iZm5e0yfmsms1tm20u2mu3Z 6.8.0-63-generic ... x86_64`，`lsb_release -ds` → `Ubuntu 24.04.2 LTS`，`date -Is` → `2026-11-17T08:27:43+08:00`。
- 本地 `C:\Users\76328\qq-deepseek`，`git log -1` → `e953315cec4620509d8bd860c26d1a530c8ab33e 2026-11-16 18:34:02 +0800 docs: design Xiaoyu personality growth and life continuity`。
- 全部侦察为读操作（`ls`/`grep`/`sed`/`cat`/`sha256sum`/`systemctl cat`），未启动、停止或写入任何服务与数据。

---

## 0. 必须先纠正的三处任务前提

侦察中发现任务书里的三条前置事实与实测不符，后续计划需按实测修正：

1. **`companion.py` 不是一个 173KB 的“主脑”文件。** 存在两个不同的 `companion.py`：
   - `/opt/qq-deepseek/companion.py`：**9064 字节**，只有 7 个顶层符号（`AlreadyRunning`/`resolve_home`/`HomeLock`/`write_console`/`run_service`/`report_error`/`main`），是启动器与 CLI 参数分派器 `[证据: ls -la /opt/qq-deepseek; grep -nE '^(class|def|async def) ' companion.py]`。
   - `/opt/qq-deepseek/life/companion.py`：**173360 字节**，才是真正的核心（`class Companion` 在第 324 行）`[证据: ls -la life/ | grep companion.py; grep -nE '^(class|def|async def) ' life/companion.py]`。
   任务书说“云端 life/ 有全套 companion_*.py … companion.py”指第二者；说“云端 companion.py 与 life/ 各路的关系”只有在区分这两者后才有意义。

2. **设计 §11 是 16 条验收场景，不是 20 条。** 逐行统计 `## 11. 验收场景`（第 243 行）到 `## 12.`（第 266 行）之间的表格数据行，共 16 行 `[证据: 设计文档 244-263 行]`。详情见 §6。

3. **云端不是“docs 只有 4 份”这么简单**：确实只有 4 份，且设计 §12 列举的 5 份交接文档全部缺失；但云端另有 `docs/companion-cloud-android-build.md`（2026-11-17 新增），该文件在设计交付后写入，含既有构建链事实 `[证据: ls -la /opt/qq-deepseek/docs]`。

---

## 1. 云端模块清单与职责

### 1.1 分层结构

```
/opt/qq-deepseek/
├── companion.py            (9064 B)  启动器/CLI：参数解析、HomeLock、异常落盘
├── life/
│   ├── companion.py        (173360 B) 核心小鱼类 Companion：决定、项目、行动、结果轮
│   ├── companion_*.py      (47 个)    companion 专用子系统
│   └── role_*.py / runtime.py 等      旧/并行角色栈（role_agent.py 189912 B）
├── toolchain/dsh/          dsh CLI 0.2.1-rc.2 (npm 安装)
├── toolchain/node/         node v22.19.0
├── vendor/dsh-runtime/     DSH Python SDK + bin.js 软链
├── mobile/android/         Android 客户端源码
├── tools/                  build-companion-android.py / cloud-build-publish.sh
└── docs/ (4 份)
```

`/opt/qq-deepseek/companion.py` 的 `run_service()` 内 `from life.companion import Companion`，`--serve` 分支走 `from life.companion_remote import run_remote_service` `[证据: companion.py:66-69 与 --serve 分支]`。因此 `companion.py` 只是入口，`life/companion.py` 是主体。

### 1.2 `life/companion.py` 的接线关系

顶层导入（第 20-53 行）显示核心直接依赖的子系统 `[证据: grep -nE '^(import|from) ' life/companion.py]`：

| 模块 | 核心职责 |
| --- | --- |
| `runtime.LifeRuntime` | 事件/仓储/作用域底座 |
| `tasks.TaskService` | 项目（project）生命周期 |
| `agent_session.SessionFailure` | 会话层错误码（`budget_unmetered` 等） |
| `spending.Spending` | 人民币预算准入 |
| `companion_model.MAX_OUTPUT_TOKENS` | 模型输出上限 |
| `companion_context`（`METHOD`/`tool_instructions`/`compact_body`/`compact_recall`） | 工具说明与上下文压缩投影 |
| `companion_continuity.Continuity` | 项目续行 |
| `companion_ambient.AmbientCollector` | 环境材料 |
| `companion_life_material` / `companion_life_view` / `companion_recollection` | 材料装配与生命视图 |
| `companion_motivation` / `companion_followthrough` | 动机/关注 与 结果后续 |
| `companion_delivery` / `companion_evidence` / `companion_presence` | 投递台账、证据、在场投影 |

运行时另挂：`ActionExecutor`（`companion_executor`）、`CompanionQQ`（`companion_qq`）、`RemoteExecutor`（`companion_remote`）、`Bilibili`/`BilibiliSocial`、`Town`、`BodyExecutor`（`companion_body`）、`ImManager`（`companion_im`）。委托（DSH）只经 `ActionExecutor`。

### 1.3 47 个 `companion_*.py` 按职责归类

`[证据: ls -la life/companion_*.py 全量清单]`

- **决策与生命周期**：`companion.py`、`companion_model.py`、`companion_context.py`、`companion_continuity.py`、`companion_followthrough.py`、`companion_motivation.py`
- **执行**：`companion_executor.py`、`companion_dsh_worker.py`、`companion_work.py`、`companion_skills.py`、`companion_computer.py`、`companion_bilibili*.py`(6)、`companion_web.py`、`companion_shell.py`、`companion_phone.py`
- **社交/通道**：`companion_qq.py`、`companion_wechat.py`、`companion_im.py`、`companion_delivery.py`、`companion_letter.py`、`companion_bilibili_social.py`、`companion_town.py`
- **认知/记忆**：`companion_memory.py`、`companion_recollection.py`、`companion_ambient.py`、`companion_life_material.py`、`companion_life_view.py`、`companion_growth.py`、`companion_presence.py`、`companion_evidence.py`、`companion_failures.py`
- **服务/远程/设备**：`companion_remote.py`、`companion_mobile.py`、`companion_mobile_audio.py`、`companion_desktop.py`、`companion_body.py`、`companion_pet.py`、`companion_tunnel.py`、`companion_connections.py`
- **观测**：`companion_observability.py`

### 1.4 常驻服务的真实形态

`systemctl cat companion.service` `[证据]`：
- `User=companion`、`WorkingDirectory=/opt/qq-deepseek`、`UMask=0077`、`KillMode=control-group`
- `ExecStart=/opt/qq-deepseek/.venv-companion/bin/python /opt/qq-deepseek/companion.py --home /var/lib/companion/xiaoyu --serve --host 127.0.0.1 --port 8765`
- 与任务书一致，仅补 `--host 127.0.0.1`。

`systemctl is-active companion.service companion-embedding.service nginx` → `active / active / active` `[证据]`。

`CompanionServer`（`companion_remote.py:104`）暴露路径 `/rpc`、`/ws`、`/mobile/`、`/companion`、`/companion/artifacts`、`/health`，鉴权为 `Authorization: Bearer <home>/data/companion/server-token`，用 `hmac.compare_digest` 比对 `[证据: companion_remote.py:130; grep 路径]`。8765 仅监听 `127.0.0.1`，对外经 nginx `[证据: ss -ltnp]`。

---

## 2. DSH 委托与预算：产品阻断的精确位置

这是设计 §3 第 14 段点名的“必须解决的产品阻断”。四处联动，缺一不可：

### 2.1 预算启用时从 `available_actions` 移除

```python
# life/companion.py:1326-1328
if self.spending.policy.enabled:
    kinds = [kind for kind in kinds if kind!='dsh']
```
`[证据: sed -n '1300,1345p']`（该段在第 1327 行）。变量随后进入 `state['available_actions']`（第 1329 行）。
→ 后果：模型看到的行动目录里没有 `dsh`，**她不知道这个能力存在**，却也不会被告知“因为预算不可计量而被隐藏”。违反设计 §5.4“不能让模型以为我不会做”。

### 2.2 执行处硬拒绝

```python
# life/companion.py:2661-2662
if action['kind']=='dsh' and self.spending.policy.enabled:
    raise SessionFailure('budget_unmetered','预算已启用；DSH 尚未提供完整逐次费用计量，本次未启动。')
```
`[证据: sed -n '2600,2800p' 第 2661-2662 行]`。该分支在 `_worker()` 中、真正调用 `executor.run` 之前，被 `except Exception` 捕获后转成 `result=dict(status='failed', code='budget_unmetered', ...)`（第 2678-2682 行）。

### 2.3 用量记账被显式豁免

```python
# life/companion.py:2764-2766
if json.loads(row['action'])['kind']=='dsh' and result.get('code')!='budget_unmetered':
    self._record_usage(dict(usage=result.get('usage'), ...), 'executor')
```
`[证据: 同上]`。即：被预算拒绝的那次**不记录**（合理），但一旦执行，`usage` 恒为 `None`（见 2.4）→ 记成“未知用量”，符合设计 §11“未知用量不写成 0”。

### 2.4 根因：SDK 不返回用量

`life/companion_dsh_worker.py:42-46` 自身注释即承认：
```python
# Generic SDK events do not account for all retries/compaction requests.
# Preserve unknown billing rather than invent a complete usage total.
Path(request['result_path']).write_text(json.dumps(dict(usage=None, model=..., finish_reason=..., session_id=...)))
```
`[证据: cat -n life/companion_dsh_worker.py]`。`companion_executor.py:186-191` 同样 `result.update(usage=None, model=self.cfg.model)` 后从 `.result.json` 覆盖 `usage`（值仍是 `None`）`[证据: sed -n '60,200p' 第 186-191 行]`。

**已核实 SDK 无用量接口**：vendored SDK 只向 runtime 发送 4 个 RPC 方法 `[证据: grep -o -E '"[a-z]+/[a-z/-]+"' client.py]`：
```
"session/prompt", "session/resume", "session/working-directory/get", "session/working-directory/set"
```
`client.py` 中不存在 `usage`/`tokenUsage`/`session/status` 字样 `[证据: grep -n -E 'session/status|usage' client.py → 无输出]`。

→ **结论（阻断点定性）**：预算阻断不可能靠“补一个 usage 字段”在 SDK 层解决。设计 §6.3 建议的 `bounded_estimate` 才是可行路径：限制并发/时长/调用次数，费用记“已知+未知”，不伪装精确账单。此项需在 B 阶段实现。

### 2.5 当前云端实际状态：DSH 其实是可用的

`grep spending /var/lib/companion/xiaoyu/config.toml` → `"spending_policy" = {"enabled" = false, "daily_target_fen" = 2000, "daily_limit_fen" = 5000}` `[证据]`。
即：**生产当前 `spending_policy.enabled=false`，所以 `dsh` 未被从目录移除、也未被拒绝**。阻断是“一旦用户开启预算就立即生效”的定时炸弹，不是当前已失效。

`dsh` 出现在可用目录的条件是 `_dsh_paths()` 非空（`companion_executor.py:80-81`），云端配置 `[证据: grep dsh config.toml]`：
```
"dsh_root" = "/opt/qq-deepseek/vendor/dsh-runtime"
"dsh_home" = "data/dsh-linux"
"dsh_node" = "/opt/qq-deepseek/toolchain/node/bin/node"
"dsh_provider" = "deepseek-official"
"dsh_timeout" = 240.0
```
且已存在真实运行痕迹：`/var/lib/companion/xiaoyu/data/dsh-linux/sessions/--var-lib-companion-xiaoyu--/session-c6daef...` 等 3 个会话目录 `[证据: find data/dsh-linux -maxdepth 3 -type d]`。→ DSH 委托**在云端确实跑通过**。

### 2.6 委托实现的其余缺陷（对应设计 §3、§6.1）

- **无子 agent 语义**：决策 schema 的 `kind` 枚举为 `['command','work','dsh','computer','body','bilibili','phone','town','web','skill','memory','growth']`，**没有 `agent`** `[证据: life/companion.py:232]`。设计的七操作（create/send/inspect/wait/cancel/resume/read_artifact）在云端 `life/` 中不存在 `[证据: grep -rn "kind='agent'|agent_task|subagent" 在 life/ 仅命中 interaction_mode.py / role_agent.py / initiative.py / work_context.py，均为旧角色栈]`。
- **单次同步执行**：`companion_executor.py` 的 `run()` 内 `await asyncio.to_thread(tree.process.wait)`（第 173 行）阻塞到子进程结束，`_worker()` 全程持 `self._execution`，主认知不能并行（违反设计 §6.1“主角色可继续聊天”）。
- **无父/子、无检查点、无续作**：worker 只传 `goal`、`cwd`、`model`、`provider`，`harness.run(request['goal'])` 一次到底 `[证据: companion_dsh_worker.py:40]`。`session_id` 只被写进 `.result.json`，**从未用于恢复**。
- **进程树终止有，取消语义无**：`ProcessTree` + `tree.stop()` 在 finally 中兜底 `[证据: companion_executor.py:166-196]`，但没有 cancel/wait/inspect 状态机。

---

## 3. 本地 vs 云端分化清单与合并策略

### 3.1 分化事实

| 维度 | 本地 `C:\Users\76328\qq-deepseek` | 云端 `/opt/qq-deepseek` |
| --- | --- | --- |
| 版本管理 | 有 `.git`，HEAD `e953315c` | **无 `.git`**（`ls -la` 无 `.git`）`[证据]` |
| `life/*.py` | 107 个 | 云端 `companion_*.py` **47 个本地完全没有** |
| `life/companion*.py` | **0 个** `[证据: Get-ChildItem life\companion_*.py → 空]` | 47 个 |
| `settings_web.py` | 有 | 无 |
| `role_agent.py` | 189163 B | 189912 B（差 749 B） |
| 核心入口 | `bot.py`（115 B）+ `run.ps1` 启动 `bot.py` | `companion.py`（9064 B）+ systemd `--serve` |
| 生产形态 | 本地 Windows 桌宠/模拟器 | Linux 常驻服务 + nginx + NapCat |
| `mobile/` | **不存在** `[证据: Test-Path mobile → False]` | 有 `mobile/android/` |
| `tools/build-companion-android.py` | **不存在** | 有 |
| `data/android-toolchain` | **不存在** | 有（build-tools-35 + platform-35） |
| `tools/*.ps1` | `bot-watchdog.ps1`、`napcat-login.ps1` | 无（仅 `.py`/`.sh`） |
| `docs/` | 17 份 | 4 份，**交集 0**（本地有 `dsh-build.md`、`life-*.md` 等；云端有 `companion-bilibili*.md`、`companion-cloud-android-build.md`） |
| `vendor/` | `dsh-bridge-work`（完整 monorepo，含 `apps/cli/src/bin.ts`、`node_modules/tsx`） | `dsh-runtime`（**仅 SDK + `lib/bin.js` 软链**）、`bilibili-watch`、`shizuku` |
| `tests/` | 100+ 个 `test_*.py`，含 `test_dsh_bridge.py`、`test_qr_login.py` | unknown（未见 `tests/` 目录） |

**关键正面事实：Python SDK 两端逐字节一致。** 本地 `vendor\dsh-bridge-work\python\sdk\src\deepseek_harness\` 与云端 `vendor/dsh-runtime/python/sdk/src/deepseek_harness/` 的 6 个文件 SHA256 全等、字节数全等 `[证据: 两端 sha256sum 前16位 + wc -c 对照：api.py 9586/7ff562d0…, client.py 23968/bc9eef92…, errors.py 660/0d380a89…, models.py 673/c8cba1cd…, office.py 8207/300d51a8…, __init__.py 526/0511648a…]`。→ **SDK 能力判断在两端通用，不需要分叉调研。**

**本地 `role_agent.py` 已有真实的 subagent 接线**，是重要可复用资产 `[证据: grep -n subagent life/role_agent.py]`：
- 第 1434 行：`if name=='subagent' and not binding['intent_ref']: raise SessionFailure('invalid_input','后台委派需要先保存一个可暂停、可恢复的打算，再发起执行')`
- 第 1452-1461：`method_refs` 转授权校验（不能向子代理转授当前不可见的方法）
- 第 1472-1483：`Budget.reserve(op)` → 写 `role_operations` → 记录 `delegate_method_sources`
- 第 530/537：订阅 `subagent.started` / `subagent.finished` → `initiative` 续行
- 第 2236：查 `tool='subagent'` 的最近调用做父调用归属

### 3.2 合并策略建议

1. **不要做双向自动合并。** 云端无 `.git`、`role_agent.py` 已有 749 B 差异、`companion_*.py` 全家只在云端。任何 `git merge` 都会把云端未跟踪文件当“本地新增”要求整体审批，且设计 §3 明确“不得 reset、clean、覆盖人物库”。
2. **确立“云端 `life/` 为 `companion_*` 的唯一真源”，按文件粒度反向导入。** 具体做法：把云端 47 个 `life/companion_*.py` + `life/companion.py` 原样取回本地作为**新基线提交**（不改内容），使本地 `git` 具备完整源码，之后所有改动在本地做、经测试、再按文件推到云端。前提：取回前须先 `sha256sum` 全量存档，作为回滚依据。
3. **本地补齐云端独有目录前，先确认不进分发包。** `mobile/`、`data/android-toolchain/`、`vendor/dsh-runtime/`、`toolchain/` 属构建产物或第三方二进制；按设计 §8.1，私钥、人物库、个人配置**不得**进源码/分发。建议这些放 `.gitignore` 或以 manifest 引用而非提交字节。
4. **`role_agent.py` 的 749 B 差异必须先 diff 再定。** 可能包含云端未回流的修复；在 diff 结论出来前，本地该文件不可被覆盖（写 `unknown` 于差异内容）。
5. **两套认知栈并存需显式裁决。** 云端生产跑 `life/companion.py`（Companion + actions 枚举）；本地有 `role_agent.py`（意图 + `role_operations` + subagent 工具）与 `role_host.py`（“Single production entry: OneBot + LifeRuntime + a complete DSH Agent”）`[证据: life/role_entry.py:1; life/role_host.py:1 文件首行]`。设计 §6.1 的七操作契约与 `role_agent` 的现有形状更接近，与 `companion.py` 的 actions 形状更远。**这是 B 阶段最重要的架构选择，需在实施计划中明确写入结论**，否则会出现第三套并行系统（设计 §8.1 明确禁止）。

---

## 4. DSH SDK / 运行时能力实测

### 4.1 版本（两端）

| 组件 | 版本 | 证据 |
| --- | --- | --- |
| DSH CLI (npm) | **0.2.1-rc.2** | 云端 `toolchain/dsh/lib/node_modules/@deepseek-ai/dsh/package.json` `"version": "0.2.1-rc.2"` |
| DSH monorepo (本地) | **0.2.1-rc.2** | `vendor/dsh-bridge-work/package.json` `"version": "0.2.1-rc.2"`，`apps/cli/package.json` 同 |
| Node | v22.19.0 | 云端 `toolchain/node/bin/node --version` → `v22.19.0`；本地 monorepo `engines.node: "^22.19.0 || >=24.0.0"` |
| Python SDK | 6 文件两端字节相同 | sha256 对照见 §3.1 |

**启动入口两端不同，会影响 `companion_dsh_worker` 的分支**：
- 云端：`vendor/dsh-runtime/lib/bin.js` 是软链 → `/opt/qq-deepseek/toolchain/dsh/lib/node_modules/@deepseek-ai/dsh/lib/bin.js`（已构建产物）。`_dsh_entry()` 命中 `lib/bin.js` 分支 `[证据: ls -la vendor/dsh-runtime/lib; sed -n '52,58p' companion_executor.py]`。
- 本地：`vendor/dsh-bridge-work` **没有** `lib/bin.js`（`Test-Path lib\bin.js` → `False`），有 `apps/cli/src/bin.ts` 且 `node_modules/tsx/package.json` 存在 → 走 `tsx/esm` 源码启动分支 `[证据: companion_executor.py:52-58 的 `_dsh_entry` 三选；本地文件存在性测试]`。

### 4.2 会话 / 消息 / 续接：**真实支持**

`api.py` 公开面 `[证据: cat -n api.py 全文; __init__.py]`：

| 能力 | SDK 实现 | 证据 |
| --- | --- | --- |
| 新建会话 | `start_session(session_id=None)` | `api.py:120-122`，默认 `session-{uuid4}` |
| **续接会话** | `resume_session(session_id)` → `client.resume_session` → RPC `session/resume` | `api.py:124-131`；`client.py:191-197` |
| 续接失败语义 | docstring：“Missing, unreadable, incompatible, or occupied sessions **raise** instead of creating a new session” | `api.py:125-127` |
| 发送消息 | `session_prompt(session_id, contentBlocks)` → 返回 `messageId` | `client.py:172-189` |
| 多模态输入 | `normalize_input` 支持 `list[JsonObject]`；`str` 转 `[{"type":"text","text":...}]` | `api.py:224-227` |
| 工作目录读写 | `session/working-directory/{get,set}` | `client.py:199-215` |
| 一次性运行 | `run(input, session_id=..., on_notification=...)` | `api.py:133-140` |

**续接的真实性已可确证**：`session/resume` 是真实 RPC 方法而非本地伪造；`companion_dsh_worker` 目前只把 `session_id` 写进结果文件、**从未回传复用**，属实现缺失而非 SDK 缺失。设计 §6.2“不得假设现有返回 session_id 即证明 SDK 支持续接”——本次已越过这一警戒线，**确认支持**。

### 4.3 事件流：真实支持，但**没有用量事件**

`Session.run()` 用 `subscribe_session_notifications(self.id)` 订阅，收集 `notification.method == "session.event"` 且 `payload.sessionId == self.id` 的 `event` 到 `RunResult.events`，直到收到 `session.status` 且 `status == "idle"` 才结束 `[证据: api.py:168-208]`。

已验证的事件类型：`agent/inbox/spliced`（输入回执）、`assistant/message`（最终回复提取）、`turn/end`（`data.reason.kind` → `finish_reason`） `[证据: api.py:211-221, 230-266]`。

**缺口**：全文无 `usage` / `token` / `cost` 字段，`RunResult` 只有 `session_id / final_response / finish_reason / events / notifications` `[证据: api.py:40-46]`。→ 与 §2.4 结论一致：**计费不可得是 SDK 层事实，不是本项目漏读**。

### 4.4 子代理：SDK 层间接支持，DSH 层**完整**

SDK 的 subagent 支持只体现在**通知路由**上 `[证据: client.py:262-264, 518-565]`：
- `subscribe_session_notifications(session_id)` 的 docstring：“Subscribe to a session **and descendants** discovered from subagent lifecycle edges”。
- `_record_session_relationship_locked` 监听 `subagent.started`，读 `payload.childSessionId` 与父，建立 `_session_parents` 映射。
- `_session_is_descendant_of` 递归判断后代；`subagent.started` / `subagent.finished` 用于过滤。
→ 即：**子 agent 是父会话事件流的自然延伸，父级无需额外 RPC 就能看见子级事件**。SDK 未暴露“创建子代理”的 API——创建是模型在会话内调用 DSH 工具完成的。

DSH 本体（本地 monorepo，云端 npm 包为已构建同版本）提供完整子代理体系 `[证据: ls packages/subagent; docs/subsystems/subagent.zh.md]`：

| 包 | 职责 |
| --- | --- |
| `subagent/` | 委派服务：提供方注册表、一次性运行、**可继续子级**、发现（`ctx.subagents`） |
| `subagent-spawn-in-process/` | 全新进程内子 agent |
| `subagent-fork-in-process/` | 从父级已完成历史派生的进程内子 agent |
| `subagent-acp/` `subagent-codex/` `subagent-claude-code/` `subagent-dsh-sdk/` | 进程外子 agent 后端 |
| `tool-subagent/` | 面向模型的委派工具（25 KB `index.ts`） |
| `tool-subagent-control/` | 向相邻 agent **发消息、中断、列出子级状态** |

核心机制 `[证据: docs/subsystems/subagent.zh.md]`：
- 统一入口 `startActivation`；`prepareContinuable`（本地子级）vs `start`（一次性外部执行）。
- `SubagentCapabilities` = `agentOptions` / `agentPreset` / `outputSchema` / **`depthLimit`**；`maxDepth` 在准入阶段校验，超过即拒绝。
- `SubagentActivation`：`childId`（稳定身份）、`messageId`、`result: Promise<SubagentResult>`、`dispose()`（“Stop and release this activation and **its owned descendants**”）。
- `listChildren(parentSessionId)` / `listDescendants()`：按父会话事件顺序返回直接子级，**不读子级日志**；历史子会话 descriptor 不可用时以 `mode: 'unknown'` 保持可发现但**不授予继续执行能力**。
- 父级目录（`subagentCatalog`）持久化、按事件顺序排序。

**对设计的直接对照**：
- 设计 §6.1 的 create/send/cancel/inspect/wait **全部有原生落点**（`startActivation` / `tool-subagent-control` / `dispose` / `listChildren`）。
- 设计 §6.1 “嵌套最多 2 层、合计并发 3” → `depthLimit` 原生支持，并发配额需在应用层用 `Budget`/信号量补 `unknown`（DSH 是否自带全局配额未核实）。
- 设计 §6.2 “默认不复制完整聊天史” → `subagent-spawn-in-process`（全新）正是默认；`fork-in-process` 才带父历史。**语义与设计要求一致**。

### 4.5 SDK 能力速查（结论）

| 设计所需 | SDK/DSH 实况 | 判定 |
| --- | --- | --- |
| 真实会话恢复 | `session/resume` RPC | ✅ 有 |
| 消息幂等 ID | `session_prompt` 返回 `messageId`；`agent/inbox/spliced` 回执 | ✅ 有 |
| 工具/事件流 | `session.event` 全量订阅 | ✅ 有 |
| 子代理创建 | DSH 侧 `ctx.subagents` + `tool-subagent` | ✅ 有（非 SDK API） |
| 子代理续接 | 可继续子级（continuable child）、冷恢复 | ✅ 有 |
| 子代理外部副作用归属 | `delivery: 'parent'｜'caller'` | ✅ 有 |
| **逐次费用计量** | **无** | ❌ 缺（阻断源） |
| 全局并发配额 | unknown | ⚠️ 待实测 |

---

## 5. 登录端（NapCat / OneBot）能力

### 5.1 部署现状

| 项 | 实况 | 证据 |
| --- | --- | --- |
| `napcat.service` | **inactive**（`systemctl is-active` → `inactive`），**disabled** | `systemctl is-active/is-enabled` |
| 服务形态 | `User=napcat`，`xvfb-run -a /opt/napcat/qq/opt/QQ/qq --no-sandbox ...`，`NAPCAT_MAIN_PATH=/opt/napcat/runtime/napcat.mjs` | `systemctl cat napcat.service` |
| OneBot 监听 | **未监听 3001**（`ss -ltnp` 无 3001 条目） | `ss -ltnp` |
| companion QQ 通道 | `data/companion/qq.json` → `{"enabled": false, "ws_url": "ws://127.0.0.1:3001"}` | `cat qq.json` |
| 账号配置 | `"bot_qq" = 3878921941`，`"owner" = 763282103` | `grep config.toml` |
| NapCat 版本 | `runtime/package.json` `"name": "napcat"`, `"version": "0.0.1"`（开发树，非发行版号） | `head -20 package.json` |

→ **当前云端 QQ 链路整体未启用**：NapCat 停用、3001 未监听、companion 侧 `enabled=false`。设计 §7 的任何实测都必须先把它拉起（属写入操作，本次未做）。

### 5.2 二维码与登录状态接口：**确实存在**

任务书要求“不能假设 OneBot 自带登录二维码接口”。实测结论：**二维码/登录状态不在 OneBot 协议里，而在 NapCat 自己的 WebUI REST API 里，且已实现**。

后端路由注册（`napcat.mjs`）`[证据: python3 正则提取 `mt.post(...)`]`：
```js
mt.all("/GetQuickLoginList", ...);  mt.all("/GetQuickLoginListNew", ...);
mt.post("/CheckLoginStatus", ...);          // ← 查登录状态
mt.post("/GetQQLoginQrcode", ...);          // ← 取二维码
mt.post("/SetQuickLogin", ...);  mt.post("/GetQQLoginInfo", ...);
mt.post("/GetQuickLoginQQ", ...);  mt.post("/SetQuickLoginQQ", ...);
mt.post("/RefreshQRcode", ...);             // ← 刷新二维码
mt.post("/PasswordLogin", ...);  mt.post("/CaptchaLogin", ...);  mt.post("/NewDeviceLogin", ...);
mt.post("/GetNewDeviceQRCode", ...);  mt.post("/PollNewDeviceQR", ...);
... (共 38 条 /QQLogin/* 路由)
```
挂载：`Ar.use("/QQLogin", mt)`，而 `Ar` 挂在 `jn.use("/api", Ar)` → **完整路径 `/api/QQLogin/{GetQQLoginQrcode,RefreshQRcode,CheckLoginStatus}`** `[证据: napcat.mjs 中 `Ar.use("/QQLogin", mt)` 与 `.use("/api", Ar)`]`。

WebUI 前端对应调用 `[证据: static/assets/qq_login-*.js 上下文提取]`：
- `C.refreshQRCode()` → 返回 `{qrcodeurl, restarting}`，失败含 `QQ_LOGIN_SERVICE_RESTARTING` 错误码。
- `C.checkQQLoginStatusWithQrcode()` → 返回 `{loginPhase, isLogin, qrcodeurl}`；`loginPhase` 取值含 `waiting_qrcode` / `reconnecting` / `ready`。
- `C.getNewDeviceQRCode(uin, ...)` → 返回 `{str_url, bytes_token}`（新设备验证流程）。

**鉴权**：`/api/auth/{login,check,logout,update_token}` + passkey + 2FA 路由存在；WebUI token 文件在 `/var/lib/napcat/webui-token`（43 字节，`rw-r--r--`）`[证据: ls -la /var/lib/napcat; xi 路由提取]`。→ 小鱼可在云端以本地 root/companion 身份读取 token 后调用，无需人工 WebUI 操作。**具体请求体 schema 未实测**（需拉起服务后真实调用），标 `unknown`。

### 5.3 OneBot 协议侧只能“查登录”，不能“取码”

`companion_qq.py` 唯一用到的 OneBot 登录相关动作是 `await self._rpc('get_login_info', {})`，用于核验 `user_id == cfg.bot_qq` `[证据: companion_qq.py:103-105]`。OneBot 标准动作里没有二维码接口——**与设计 §7.3 的预期完全一致**。

### 5.4 现有接收面：仅主人私聊

```python
# life/companion_qq.py:129
if (event.get('post_type')!='message' or event.get('message_type')!='private'
    or str(event.get('user_id'))!=str(self.cfg.owner) or ...): return
```
`[证据: grep -n "message_type" companion_qq.py → 仅第 129 行]`。→ **群聊、他人私聊全部被静默丢弃**。设计 §7.1“接入私聊和群聊”为零起点。

连接状态机已有：`disabled → disconnected → verifying → connected`，`qq_account_mismatch` 时会拒绝并记 `self.error` `[证据: companion_qq.py:106-118]`。设计 §7.2 要求的 `login_required` 状态**不存在**。

### 5.5 本地已有扫码窗口（可复用线索）

本地 `tools/napcat-login.ps1`（7622 B）实现了 Windows 侧扫码窗口：`Test-QrLoginReady` 以 3001 端口可连判定登录成功、`Read-QrLoginImage` 以共享读方式打开 NapCat 产出的 PNG、`Close-QrLoginWindow` 在就绪后自动关窗 `[证据: cat /tmp/dsh-sync-probe/tools/napcat-login.ps1（云端副本）]`。本地 `tests/test_qr_login.py` 以 `tests/qr_login_probe.ps1` 做真实窗口验证（`@unittest.skipUnless(os.name=='nt')`）`[证据: tests/test_qr_login.py:7-19]`。
→ 设计 §7.3 说的“可由运行 QQ 的 Windows 节点受控捕获登录界面二维码”**已有可用实现**，可作为 `needs_manual_help` 的回退路径。

---

## 6. 设计 §11 验收场景覆盖矩阵

**先纠正条数**：设计 §11（第 243-263 行）共 **16 条**场景，不是 20 条 `[证据: 设计文档 244-263 行逐行]`。下表按原文 16 条逐条给出云端代码落点与覆盖判定。

状态口径：`✅ 已覆盖`（有实现且有测试）/ `⚠️ 部分`（有实现无测试，或仅部分）/ `❌ 未覆盖` / `unknown`。

| # | 场景（原文缩写） | 云端落点 | 覆盖 |
| --- | --- | --- | --- |
| 1 | QQ 私聊/群聊问“你现在在哪、用谁的号” | `companion_qq.py:103` `get_login_info` 核验；`companion_presence._presence` 只报 desktop/phone 连接，**不报 QQ 账号与会话**；群聊消息在第 129 行被丢弃 | ❌ |
| 2 | 同昵称两人，其后一人改昵称 | 云端无 actor_ref/conversation_ref 模型；`life/people.py` 是否有稳定人物 ID 为 `unknown`（本次未读） | unknown |
| 3 | 私聊 A 后去群 B，再问刚才聊过什么 | 无跨会话检索；群消息不入库（第 129 行）；`life/people.py` 的 audience 旧策略未核 | ❌ |
| 4 | 改向另一会话回复或主动联系 | `companion_qq.emit` 只把 assistant 消息投到唯一 owner（`_outbox`）；无目标选择；`companion_delivery.queue` 有 `conversation_ref` 形参但 QQ 侧未用 | ❌ |
| 5 | 长期关注一个群，同时与主人聊天 | 无 conversation 级 attention。`companion_presence._attention` 只有 `chosen_next`/`last_timer_wake`（第 160-165 行），是**唤醒调度**而非关注模式 | ❌ |
| 6 | 创建开发 agent 后继续闲聊 | 无 `agent` kind（schema 第 232 行）；`_worker` 内 `await self._execution` 阻塞单执行（第 2670-2672 行） | ❌ |
| 7 | 运行中补充“保留旧设置” | 无 send/幂等 message_id；`companion_actions` 只有 pending/running/settled 状态机 | ❌ |
| 8 | 重启主服务/执行节点失联 | `_worker` 启动时 `UPDATE companion_qq_delivery SET status='unknown'`；`companion_delivery.reconcile_on_start` 有对账；但无 interrupted 状态、无节点租约 | ⚠️ |
| 9 | 子 agent 报“完成”但缺产物或哈希不符 | 无 verification 维度；`_settle` 只看 `result['status']`（第 2737-2766 行） | ❌ |
| 10 | 子 agent 改自身工具出现坏版本 | `dsh_changes.py`（`EditJournal`/`queue_change`）+ `services.dsh_workshop` 存在，设计 §3 认可其为既有基础；健康检查+回滚未核实 | ⚠️ |
| 11 | 开预算后调用 agent | **断点在此**：第 1327 行移除 + 第 2661 行拒绝 + SDK 无用量（§2） | ❌（已定位） |
| 12 | QQ 短暂断网 vs 真实登录失效 | 断网有 `_connect` 循环+3 秒退避（第 106-118 行）；**无 `login_required` 状态、无扫码触发** | ⚠️ |
| 13 | 二维码过期/手机离线/刷新/多端查看 | `/api/QQLogin/RefreshQRcode` 与 `CheckLoginStatus` 存在（§5.2），但 companion 侧**零接线** | ❌ |
| 14 | 扫码登录了错误 QQ | 有 `qq_account_mismatch`（第 104 行）；但不存在“登录会话”概念，无法把错误账号关联回某次求助 | ⚠️ |
| 15 | Android 覆盖安装 / Windows 干净环境安装 | Android：`cloud-build-publish.sh` + 证书指纹 `fb83594c...` 已有真实证据（`docs/companion-cloud-android-build.md` 记 `Xiaoyu-0.4.7.apk`, 176939 B, v2/v3 通过）；**Windows 干净环境**：本地无 `build-companion-launcher.ps1`（`Get-ChildItem -Filter '*launcher*'` → 空），设计 §3 表格点名的该脚本**本地与云端均未找到** | ⚠️ |
| 16 | 上传成功但下载内容损坏 | 脚本上传后会 `sha256sum` 并 `apksigner verify --print-certs`（`cloud-build-publish.sh`）；**无“客户端方式实际下载并核对”** | ⚠️ |

### 6.1 汇总

- 16 条中：**✅ 0 条**；**⚠️ 6 条**（#8、#10、#12、#14、#15、#16）；**❌ 9 条**（#1、#3、#4、#5、#6、#7、#9、#11、#13）；**unknown 1 条**（#2）。
- 9 条 ❌ 集中在三簇：**多会话/多人物认知（1、3、4、5）**、**子 agent 闭环（6、7、9、11）**、**扫码求助（13）**——正好对应设计 §10 的 A/B/C 三个阶段，无意外。
- ⚠️ 的 6 条中，#8/#12 有真实基础设施但缺状态语义；#15/#16 是发布链“有构建、缺验收”。

---

## 7. 其余边界事实

### 7.1 Windows 启动器与 Android 构建链

- **Android（云端可跑）**：入口 `python3 tools/build-companion-android.py`，发布包装 `bash tools/cloud-build-publish.sh`（`set -euo pipefail`，产出装到 `/var/www/companion/downloads`，默认 `PUBLIC_BASE=https://47.104.8.84/downloads`）`[证据: cat cloud-build-publish.sh; head -40 build-companion-android.py]`。工具链在 `data/android-toolchain`，签名密钥 `/root/.android/xiaoyu-signing`。设计 §8.2 的 `built/verified/published/download_verified/device_verified` **五维度在云端代码中不存在**（`grep verified release.py` 仅命中 `release.py:338` 的 `PermissionError("unverified_worker_entrypoint")`，语义无关）。
- **本地**：**没有** `mobile/`、`tools/build-companion-android.py`、`data/android-toolchain`（三项 `Test-Path` 全 False）。→ **Android 构建链目前只在云端可执行**，本地无法出包。这与设计 §2“主要维护自己的 Windows/Android 客户端”存在落差，需在计划中列为环境缺口。
- **Windows 启动器**：本地入口为 `一键启动.bat` → `run.ps1`（NapCat + adb 连模拟器 + `bot.py`）；`status.ps1` 检查 3001/6099 端口与 `bot.py` 进程；`stop.ps1` 处理启动器双 PID 孤儿问题 `[证据: 三脚本读取]`。**这些启动的是 `bot.py`（旧角色栈），不是 `companion.py`** —— 本地与云端是两套不同入口，合并时必须明确本地走哪一套。
- 设计 §3 点名的 `tools/build-companion-launcher.ps1`：本地 `Get-ChildItem -Recurse -Filter '*launcher*' -Depth 3`（排除 `node_modules`/`napcat`）→ **空**。`unknown`：该脚本可能从未提交，或位于更深路径。

### 7.2 云端 docs 与交接文档缺口

设计 §12.1 要求先读 5 份文档，云端现状 `[证据: ls -la /opt/qq-deepseek/docs]`：
- `docs/companion-qq-design.md` — **不存在**
- `docs/companion-message-routing.md` — **不存在**
- `docs/companion-continuity.md` — **不存在**
- `docs/companion-followthrough.md` — **不存在**
- `docs/companion-autonomous-development-handoff.md` — **不存在**

实际只有 `companion-bilibili-login.md`、`companion-bilibili.md`、`companion-bilibili-social.md`、`companion-cloud-android-build.md`。→ 设计 §12.1 的“先读”前提不成立，实现者只能从源码重建理解（本报告即为此服务）。建议 A 阶段先补这 5 份的替代说明。

### 7.3 数据与安全边界（供后续阶段参照）

- 生产库 `/var/lib/companion/xiaoyu/data/life.db` = **343465984 字节**（约 328 MiB），最后修改 `Nov 17 08:29` `[证据: ls -la data/life.db]`。任何测试都不得指向它。
- 人物家目录 `drwx------`，`config.toml` 为 `-rw-------`（0600）`[证据: ls -la /var/lib/companion/xiaoyu]`。本报告未复制任何凭据正文。
- `/opt/qq-deepseek` 下存在 `life.bak-*` 三份备份目录（`life.bak-20261116-021713`、`life.bak-fix-20261116-101221`、`life.bak-work-20261116-110559`）`[证据: ls -la /opt/qq-deepseek]`。回滚可用，但**从中挑选基线前必须逐一核对时间戳与内容**，不能默认最新即最好。

---

## 8. 给实施阶段的直接结论

**可以立刻依赖的事实（有证据）**

1. DSH 的会话续接、事件流、子代理（含可继续子级、深度上限、进程内/外后端、控制工具）**真实存在**，版本 `0.2.1-rc.2`，两端 SDK 字节一致。
2. 预算阻断的根因**已定性为 SDK 不提供用量**（只有 4 个 RPC，无 usage 字段），因此 `strict` 模式对 DSH 结构性不可行，`bounded_estimate` 是唯一可行方向。
3. NapCat 有完整 `/api/QQLogin/*` 二维码与登录状态接口（取码/刷新/查状态/查信息共 38 条路由），云端已安装、当前**停用**。
4. 现有 QQ 接收面只接主人私聊一行判断（`companion_qq.py:129`），群聊/他人为零。

**必须先决策再动手**

1. **认知栈裁决**：`life/companion.py`（actions 枚举，云端生产）vs `life/role_agent.py`（意图+role_operations+subagent，本地已有真实委派）。设计 §8.1 禁止再造并行系统，二者只能选一为主。
2. **合并方向**：建议以云端 `life/companion_*.py` 为真源回导本地 git，而非反向推送；回导前全量 `sha256sum` 存档。
3. **本地环境缺口**：本地缺 `mobile/`、`tools/build-companion-android.py`、`data/android-toolchain`，Android 任务在本地无法验证，只能云端跑。

**无法给出结论、需实测的项（unknown）**

1. NapCat `/api/QQLogin/*` 的请求体 schema 与实际返回——需拉起服务后真实调用（写入操作，本次未做）。
2. DSH 是否自带全局并发配额（设计要“合计 3、嵌套 2”）——`depthLimit` 覆盖嵌套，并发未核。
3. `life/people.py` 的 actor 稳定性与 audience 策略是否阻断跨会话检索（设计 §5.5 专门点名）。
4. `role_agent.py` 两端 749 B 差异的具体内容。
5. `tests/` 在云端是否存在（未见目录）。
6. `docs/companion-cloud-android-build.md` 记录的真实构建证据未被复验（未执行 apksigner）。
