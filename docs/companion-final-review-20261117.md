# P11 终局独立复核（t18）：验收清单与线上文件一致性 + D3 链路判定

评审人：reviewer（独立评审，只读；未改产品代码、未重启服务、未写生产库、未发 QQ、未向云端写文件）
时间：2026-11-17 17:26–18:05（Asia/Shanghai）
被评对象：t16（P9 收口）+ t17（P10 D3 真实委托）+ t19（P12 DSH 启动路径）
复核方式：全部数值由我从云端现场重算（`sha256` / 只读 SQLite / `journalctl` / `stat`），
不引用实现者在报告里写的数字；引用到的报告内容都标注为"实现者自述"。

---

## 0. 结论

**verdict = pass**（清单与线上逐字节一致，6 条必做全部成立）。但 pass 不等于"所有事都做完"：
`③ 仍未验证` 一栏里还有 6 项未消，其中 **2 项对用户可见**（费用不可对账、凭据暴露面），
建议它们在 D 阶段发布前作为**告知项**交给用户，而不是当成"已经没问题"。

一句话证据：**最新清单 `20261117-171616-t19-final` 共 191 行，我逐行对线上取 sha256，
191/191 全部命中，0 失配 0 缺文件 0 重复路径**；旧清单两份都只多了 `SUPERSEDED.md`、
历史文件未被追加；部署前恢复点与部署后快照分开存放并各自注明；t16/t17 期间的每一次
代码变更都新开了清单（无热修、无追加）。

---

## 1. 三栏（用户口径，先给结论）

### ① 模拟测试通过（假模型 / 隔离环境）

| 项 | 状态 | 证据（本次复核） |
|---|---|---|
| 状态机 / 守卫 / 幂等 / 并发 3 / 嵌套 2 | ✅ | 上一轮（t14）我在云端真 `AgentManager` 上重跑：层级 0/1/2、第 4 层 `nesting_limit`、同名 `task_ref` → `parent_ambiguous`、裸数字会话 → `conversation_ref_ambiguous` |
| 预算三模式 | ✅ | 本轮我在线上代码上重跑 `SpendingPolicy.parse`：`enabled` 无 mode → `strict`；`mode=bounded_estimate` → `bounded_estimate`；`mode=off` → `off`；非法值 `bogus` → `ValueError invalid_spending_policy_mode` |
| `send` 不得说成"已收到" | ✅ | 本轮实测：`delivered=False`、`applied='stored'`、`applied_reason='worker_does_not_read_stdin'`、stdin **写入 0 字节**、文案"已入台账…但 worker 尚未读取"；重复 `message_id` → `applied_reason='duplicate_stored'` |
| 未知用量不写 0 | ✅ | 线上 `companion_usage`：`usage='null'` 43 行；`input_tokens=0 且 output_tokens=0` 的行 **0 条** |
| 离线回归 | ✅ | 实现者自述云端 `tests/test_agent_repair.py` 28 条 OK（`evidence/run_repair.log`）；本轮我未重跑（云端 `tests/` 属主 root，只读复核不执行） |

### ② 真实链路通过（有完整证据的才计入）

| 项 | 状态 | 证据（本次复核） |
|---|---|---|
| D3：生产入口 → 真实 DSH 子 agent → 结果回小鱼 | ✅ **基本链路通过（1 轮成功 + 1 轮同结论）** | `20261117-172329-d3/d3-result.json` 13 项 12 过；真实子进程 929446 `companion_agent_worker.py`；`session_id=session-d200482d…`；token `D3-34230CDD0FD7` 在回执与日志里都出现；探测目录跑完仍只有 `probe.txt` |
| 走的是生产入口而非直调 executor | ✅ | `tools/stage-c/d3_real_delegation.py:181-194`：`life.ingest_message` → `core._run_cognition(…, 'dialogue', …)`（真决策回合）→ 落 `pending` → `core._agent_pending_one()`；`core._agent_worker_one()` 走台账。不是 `executor.run` 直调 |
| 结果轮被唤醒 | ✅ | `20261117-172329-d3`：`companion_wake/self_observation` 事件 + `companion_activity` 出现"子 agent 有了结果：completed" |
| 生产零污染（D3 全程） | ✅ | 我现场只读：生产 `companion_agent_tasks=0`、`companion_agent_events=0`、`kind='agent'` 动作 **0 条**；`config.toml` sha256 `f5e16926…` 未变；`ActiveEnterTimestamp=17:16:16`、`MainPID=923810`、`NRestarts=0` 未重启 |
| 最新清单 ↔ 线上 | ✅ | 191 行逐行重算：**191 MATCH / 0 MISMATCH / 0 MISSING**；重复路径 **0**；覆盖 `life/` 全部 158 个 `.py` + `tests/` 1 + `tools/` 15 + `.acceptance/` 17 |

### ③ 仍未验证（如实保留，不进"通过"）

1. **长期稳定性**：并发（≥3 件同时跑）、排队、`resume`、超时中止、进程被杀后的收尾——一条都没验。
   两次 D3 成功只证明**基本链路可用**，不能据此写"稳定"。
2. **`verification` 拿不到 `passed`**：D3 的台账是 `status=completed / verification=pending`
   （`artifacts=[]` → 无证据不给 passed）。这是"没有可核验的东西"还是核验逻辑缺口，**仍需实现者确认**。
   在此之前，"完成"这个结论在台账上永远停在 `pending`。
3. **费用不可对账**：DSH 不返回逐次用量 → `usage="null"`；预算 `off` 下 `spending_calls` 不记账
   （我现场确认 `spending_calls` 前后都是 10828，没有新增）。**这次真实花费仍是未知数**，
   费用账本里查不到。
4. **凭据暴露面**：子 agent 以 `DSH_PERMISSION_MODE=danger-full-access` 运行，且 `_dsh_env()` 把
   `cfg.api_key` 注入子进程环境——**子 agent 在自己的会话里能读到这个 key**。本轮 goal 限死只读、
   结果也未越权，但这是结构性风险。
5. **宽委托未验**：真实模型在更宽目标（多步、多文件）下的表现未测；本轮 goal 极窄。
6. **"禁止嵌套"只靠提示词**：产品侧没有硬拦。

---

## 2. 五档证据（分开写，不合并）

| 档 | 阶段 A（认知层） | 阶段 B（子 agent） |
|---|---|---|
| ① 代码已实现 | ✅ 云端=本地逐字节一致（MD5 全等） | ✅ 同上；`life/` 全部 158 个文件在清单内 |
| ② 离线测试通过 | ✅ 阶段 A 单测在云端 `tools/stage-a/` | ✅ `test_agent_repair.py` 28 条 OK |
| ③ 在线接通 | ✅ 生产进程 17:16:16 起在跑；`read_backend` 每轮被写 | ✅ 决策入口 → 认领 → 台账 → 执行 → 回小鱼 全链在真 `Companion` 上走通 |
| ④ 真机验收 | ✘ 未做（认知层不需要） | ⚠ **部分**：1 轮成功 + 1 轮同结论，**只证明基本链路**；并发/续接/超时/被杀 全未验 |
| ⑤ 实际发布 | ✅ 已上线；清单与线上 191/191 一致 | ✅ 同上 |

> 按口径：④ 只能记"部分"，不得记"真机验收通过"。

---

## 3. 六条必做的逐条核对

### 3.1 清单与线上逐字节核对 —— ✅
- 最新 final 清单：`.acceptance/20261117-171616-t19-final/manifest/SHA256SUMS`，191 行、每路径一行。
- 我逐行 `sha256(open(live))` 比对：**MATCH 191 / MISMATCH 0 / MISSING 0**；重复路径 0；
  `manifest/duplicates.txt` 为空、`VERDICT.txt` 写 `UNIQUE_PATHS_ONLY`、`verify.txt` 写 `ALL_MATCH`。
- `snapshots/` 里 7 个产品文件与线上 **逐一相同**，且都在清单里（`snapshots/README.md` 注明是"部署后快照"）。

### 3.2 旧清单处置 —— ✅
- `.acceptance/20261117-162149-final/SUPERSEDED.md`：指向 t19-final，写明原因（t19 改了 worker/agents 的 sha256），
  声明"保留不删、历史文件未追加或改写，只新增本标记文件"。
- `.acceptance/20261117-144013-repai/SUPERSEDED.md`：指向 t16-final，措辞同上。
- 我抽查旧目录：两份目录各自"文件数 + 1（SUPERSEDED.md）"，无其它新增；旧 `after.sha256` 未被追加
  （旧目录里 `after.sha256` 5140 B 与上一轮评审时一致）。

### 3.3 备份分类 —— ✅
- 部署**前**恢复点：`.acceptance/restore-points/20261117-161739-before-t16/`、
  `.acceptance/restore-points/20261117-171534-before-t19/`，各自带 `SHA256SUMS`、`stat.txt`、`README.md`，
  **放在 final 目录之外**（不会被误当"当前版本"）。
- 部署**后**快照：`<final>/snapshots/`，`README.md` 写明"部署后快照"，与恢复点分开。
- 我核对了链条：`before-t16 = 853bb29e…`（= t14 评审时的线上版本）→ t16 快照 `ab905d90…`
  → `before-t19 = ab905d90…`（正是上一步的产物）→ 当前 `73e6f5e8…`。**链条闭合**。

### 3.4 热修纪律 —— ✅
- 我按 mtime 扫 `life/`：**没有任何产品文件晚于清单生成时间 17:17:17**；产品文件最后修改
  17:15:34（t19 部署），清单 17:17:17 生成，盘点在上。
- t16 → t19 的两次代码变更**各自新开了一份清单**（`162149-final`、`171616-t19-final`），
  旧目录只加 `SUPERSEDED.md`，**没有向旧清单追加**。
- 上一轮（t14）发现的"部署后又热修、after.sha256 对不上"在 t16 之后**未再复发**。

### 3.5 D3 链路逐条（a–f）—— ✅（含两点保留）

| 条 | 结果 | 我的核对 |
|---|---|---|
| a 委托被接收 | ✅ | `(a1)` `action_id=companion-action_893ce278…`、`pending`、`kind=agent`；`(a2)` `_agent_pending_one` → `claimed=true`、动作行 `queued`、台账 `queued`、`origin=dsh` |
| b worker 实际执行 | ✅ | `(b1)` 22.0 s / 预算 300 s 未超时；`(b2)` 真子进程 `929446 … companion_agent_worker.py …request.json`；`(b3)` 模型真实回复含 token；`(d1)` `session_id=session-d200482d…` |
| c 结果返回小鱼 | ✅ | `companion_wake/self_observation` 事件 + activity「子 agent 有了结果：completed」 |
| d 状态落库 | ✅（保留：`verification=pending`） | `status=completed`、`attempt=1`、事件序列 `created→receipt→running→completed`；`artifacts=[]` 所以不给 `passed` |
| e 预算与费用来源 | ✅（保留：不可对账） | `companion_usage` 有 `purpose=executor / model=deepseek-flash / usage="null"`；无未知写 0（全表 0 条）；`spending_calls` 前后 10828 不变 |
| f stored 不得说成已收到 | ✅ | 本轮我实测 `send`：`delivered=False / applied='stored' / applied_reason='worker_does_not_read_stdin'`；D3 本身没走 `send` |

关于 `(c1)` 那条"红"：我核对了代码与数据。`_agent_pending_one()` 用 `executor.run()`（内部
`AgentManager.dispatch(op=create)`）把回执（`queued`）写回**动作行**，真正的执行由
`_agent_worker_one()` 走**台账**完成。所以动作行停在 `queued`、台账变 `completed` 是**当前设计**，
`(c1)` 断言选错了层级。实现者没有为了变绿改判据——这一点做得对。

### 3.6 部署权限与属主 —— ✅（有一点保留）

| 路径 | 属主 | 判断 |
|---|---|---|
| `life/`（产品代码） | `companion:companion` | ✅ 正确 |
| `.acceptance/20261117-162149-final/`、`171616-t19-final/`、`restore-points/` | `companion:companion` | ✅ 已修正 |
| `.acceptance/20261117-144013-repai/` | `companion:companion` | ✅ 已修正（上一轮 B1 的落点） |
| `tests/` 目录 | `root:root`（`test_agent_repair.py` 单文件 `companion`） | ✅ 按 t16 声明"只改影响面内两个落点"，**未扩大** |
| `tools/stage-c/`（D3 脚本） | `root:root` | ⚠ 保留项：D3 脚本归 root 不是"写产品文件"，属可接受的取证侧特权；但见 M2 |
| `/var/lib/companion/xiaoyu/config.toml` | 已是 `companion:companion` | ⚠ 见 M1 |
| 项目根其它（`mobile/`、`docs/` 等） | `root`（既有） | ✅ 未扩大改造面 |

特权重启与文件落地是分开的：t16/t19 都先用 `install -o companion -g companion -m 644` 落文件
（`life/` 全体 companion 属主可证），再单独 `systemctl restart`；服务 `ActiveEnterTimestamp`
与我现场读到的 `MainPID=923810 / NRestarts=0` 一致。

---

## 4. Finding

### blocker
**无。** 6 条必做全部成立，清单 191/191 与线上一致。

### high

**H1：`verification` 拿不到 `passed`，"完成"在台账上永远是 `pending`**
- 文件：`life/companion_agents.py`（`_verify()` / `_manifest_from_run()`），D3 台账证据 `20261117-172329-d3/d3-result.json` `(d1)`
- 问题：`artifacts=[]` 时 `_verify()` 返回 `None`，`verification` 保持 `pending`。于是任何"只读、不产出文件"
  的子 agent 工作——也就是最安全的一类——**永远拿不到 `passed`**；而 `agent_capability` 的 ready 判定
  要求"台账里有 completed 且 passed 的记录"，两者互相卡死：能力目录里 `agent.subagent` 会长期停在
  `needs_setup`。
- requiredFix：给"无交付物"的活一条可核验的通过路径（例如 `tests` 里有一条运行期自检即视为
  `passed`，或把"只读任务"的核验口径写成 `verified_readonly` 并让能力目录认它），并把结论写进契约；
  在当前状态下，任何"子 agent 能力已就绪"的说法都不成立。

**H2：本次真实调用的费用不可对账**
- 文件：`life/companion.py`（`_settle` → `_record_usage`）、`life/spending.py`（`reserve()` 在 `policy.enabled=False` 时直接放行不记账）
- 问题：`usage="null"`、`spending_calls` 不新增（我现场确认前后都是 10828）。**花了多少钱，事后查不到**。
  这对"发布闭环"是实质缺口：一次真实模型调用在费用账本里不可见。
- requiredFix：把"未知用量"也记一条 `spending_calls`（state=`unknown`，金额记 null 而不是 0），
  让每次付费调用至少"可见"；或在报告里把该次调用作为"账外消耗"单独登记。

### medium

**M1：本轮部署又一次动了生产 HOME 的属主**
- 文件：`/var/lib/companion/xiaoyu/config.toml`
- 问题：上一轮（t14）它是 `root:root`；本轮我读到的是 `companion:companion`（sha256 未变 `f5e16926…`）。
  修好了，但**修正动作本身又是一次对生产 HOME 的写入**，且没有出现在任何报告里。
- requiredFix：在 `docs/cloud-sync-policy.md` 里补一条"部署前后各取一次生产 HOME 的属主快照并留证"，
  把这类"顺手修正"纳入证据，而不是靠评审发现。

**M2：`tools/` 下的可复跑脚本不进清单、属主 root**
- 文件：`tools/stage-c/`（51 个文件）、`tools/stage-c/d3_real_delegation.py`、`tools/stage-c/d3_initialize_check.py`
- 问题：最新清单的 15 条 `tools/` 全部是 `stage-b/` 的；**D3 的复跑脚本、预检脚本一个都没进清单**。
  报告里引用的"复跑：`sh tools/stage-c/d3_run.sh real`"因此**无法用清单验证它没被改过**。
- requiredFix：把 `tools/stage-c/` 的取证脚本纳入下一份清单（或单独出一份 `TOOLS-SHA256SUMS` 并在
  REPORT 里指向它），让"可复跑"是可以被独立验证的。

**M3：`.acceptance/LATEST` 仍然指向 09:08 的旧目录**
- 文件：`/opt/qq-deepseek/.acceptance/LATEST`（内容 `20261117-090800`，mtime 09:08）
- 问题：下游（发布、日报、D 阶段）按 `LATEST` 取证据会拿到当天最早、早已被替代的那份。
- requiredFix：`echo 20261117-171616-t19-final > .acceptance/LATEST`，并把"每次新开清单必须更新 LATEST"
  写进策略文件的检查清单。我在 `.acceptance/20261117-144013-repai` 那轮提过一次，仍未执行。

**M4：根目录有一个与代码无关的空文件 `DIRECT`**
- 文件：`/opt/qq-deepseek/DIRECT`（0 字节，mtime 15:05，`root:root`）
- 问题：像是 heredoc 重定向写错留下的残渣（`cat > DIRECT <<EOF`）；不影响运行，但属于部署过程留下的垃圾。
- requiredFix：确认无引用后删除，并在部署脚本里加"落盘后检查根目录没有新增意外文件"。

### low

**L1：`d3-run` 的 `chown` 与 `staging-t16/`、`p8-*`、`163557/163643-d3` 等目录仍是 `root:root`**
这些是历史取证目录，本轮声明"未在影响面内、未改"——合理；但 `.acceptance/staging-t16/` 是临时暂存目录，
建议在下一轮清理，避免与正式清单混在一起被误取。

**L2：D3 报告与云端 `tools/stage-c/companion-d3-real-delegation-20261117.md` 是同一份（9397 B）**
放在脚本目录里，与 `docs/` 里的版本容易分叉；建议 `tools/` 只留脚本，文档只在 `docs/` 与 `.acceptance/`。

**L3：`20261117-163557-d3` / `163643-d3` 两个目录没有 `REPORT.md`**
它们只有原始快照，没有结论说明；建议补一句"被 163750 取代"的标记，免得后人以为缺报告。

---

## 5. 上一轮（t14）finding 的现状

| 编号 | 内容 | 现状 |
|---|---|---|
| B1 | 生产 config.toml / .acceptance 属主 root | **已修**（现有目录与 config.toml 都已回到 companion；但修正动作未留证 → 见 M1） |
| B2 | 部署后热修导致 after.sha256 对不上 | **已修**：t16/t19 各自新开清单，`life/` 无晚于清单的文件，191/191 全对 |
| H1 | send 标 delivered 但 worker 不读 stdin | **已修**：实测 `delivered=False / applied='stored' / applied_reason='worker_does_not_read_stdin'`，stdin 0 字节 |
| H3 | config.toml 无法选 bounded_estimate | **已修**：`SpendingPolicy.mode` + `effective_mode()`，实测三值+rules，非法值抛 `invalid_spending_policy_mode` |
| H2 | 无新 awareness 装配证据 | **未复验**（本轮清单只保证字节一致，不含装配证据） |
| H4 | stage-a 旧副本 / 证据入口缺失 | **部分修**：stage-a 测试脚本已补；但新的 D3 脚本仍不进清单（M2） |
| M1 | 验收未证明未写生产库 | **已修**：D3 前后 ledger 计数一致（0/0，usage 3091、spending 10828 均未变） |
| M2 | t9 B3 仍红 | **未修**（仍用重复 task_ref 当父，按设计 `parent_ambiguous`） |
| M3 | LATEST 未更新 | **未修** |
| M4 | 并发先读后领 | **未修**（D3 只跑 1 件，未触达） |
| M5 | 结果轮 action_result 重复关键字 | **未观测**（D3 结果轮跑起来了，但没验证关键字冲突） |
| L1/L2/L3 | context.py CRLF / 死代码 / 指纹误读 | L2 已修（`_legacy_spending_mode` 删除，我确认线上无该符号）；L1 未修；L3 已缓解 |

---

## 6. 给用户的告知（放进最终交付报告）

1. **子 agent 现在能力可用，但"完成"不等于"核验通过"**：台账会写 `completed`，`verification` 停在
   `pending`（H1）。要拿到 `passed`，得先补"无交付物任务"的核验口径。
2. **这次真实委托花掉的钱查不到**（H2）：用量是 `null`，费用账本里没有这一笔。若后续要做预算约束，
   这条必须先补。
3. **子 agent 能看到 API key**（③-4）：`danger-full-access` + 环境变量注入。本轮 goal 限死只读、
   结果也未越权，但这是一条结构性风险，不是一次验收能消除的。
4. **长期稳定性未验**（③-1）：只跑通了两轮单件委托；并发、续接、超时、被杀收尾全都没验。
