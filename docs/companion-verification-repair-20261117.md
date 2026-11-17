# P8 修复轮证据回收与隔离复验

- 验收者：isolation-verifier（t15；二级验收）
- 验收时间：2026-11-17 16:03:40 – 16:06 CST
- 验收对象：云端生产基线 `/opt/qq-deepseek`（P6 修复轮已在 15:24:13 重启上线）
- 证据：云端 `/opt/qq-deepseek/.acceptance/p8-20261117-1610/`，本地 `docs/evidence/p8-20261117-1610/`（同一份）
- **本轮没有改产品代码、没有重启服务、没有写 `/var/lib/companion`、没有连真实模型或 QQ。**

## 0. 结论

| # | 必做项 | 结论 |
|---|---|---|
| 1 | 100 条判定复跑，A6 / B3 转绿，其余无新失败 | **A6 已转绿；B3 那一行仍红，但不是同一件事**（见 §2）；其余无新失败 → 98 通过 / 1 失败 / 1 受限 / 51.7 s |
| 2 | 阶段 B 执行入口端到端（真 Companion + 假模型，`kind=agent/op=create`） | **链路通了一半**：`executor.run` 这一环真修好了；但「模型提交 → 动作入库 → 送上执行」这一段没有把 agent 动作转进台账（§3，失败） |
| 3 | 预算三模式在真实配置对象上的行为 | **全部通过**（§4） |
| 4 | 生产核对 / 无残留 / 清理回执 | **全部通过**（§5） |

一句话：修复轮把 `executor.run(kind=agent)` 这一环接上了，也把 A6（裸数字会话引用）和嵌套深度都修对了；
但模型经由决策流程派出的子 agent 仍然**到不了台账**——它停在 `companion_actions` 的待办里没人领。
这是 F2「模型侧动作提交到队列」这条链上剩下的最后一环，也是本轮**唯一需要下游处理的失败**。

> **阅读提醒（裁定结果）**
> - **B3 那条 `ok=false` 不是回归、不是产品缺陷**，是 t9 探针写法 + 一条新的正确守卫（`parent_ambiguous`）。
>   captain 已裁定「探针写法问题」，`tools/stage-c` 判定条件不改。为什么它其实是「守卫在工作」，见 **§2.2.1**。
> - **F2-d / F2-j 那两条 `ok=false` 才是产品缺陷**，定位与修法见 **§3.1**。

## 1. 隔离条件（与 t9 同一套规矩）

| 项 | 做法 |
|---|---|
| 工作区 | `/tmp/p8work-*`（脚本要 `copytree` 源码树，**不能**放在生产树或归档目录里；见 §6 事故与修复） |
| 源码树 | 副本 `/tmp/p8work-*/matrix|e2e/tree`，脚本在副本里跑；`toolchain/`、`vendor/`、`data/`、`.acceptance/` 不拷 |
| 库 | 每个场景全新空库；生产库全程未被打开写 |
| 模型 | `DecisionModel` / `FakeModel`——不连任何真实模型 |
| 平台 | `QQ_OFFLINE=1`、`COMPANION_FORBID_EXEC=1`；子 agent 只用离线 `node=echo` |
| 凭据 | 13 个凭据环境变量从进程中删除；沙箱 `config.toml` 的 `ws_token/api_key` 置空 |
| 生产服务 | 全程 `active`、`MainPID=864221`、`NRestarts=0`、`ActiveEnterTimestamp=15:24:13`（未动） |

## 2. 必做 1：100 条判定复跑

命令（云端）：`sh /opt/qq-deepseek/tools/stage-c/p8_collect.sh /opt/qq-deepseek/.acceptance/p8-20261117-1610`

**结果：`{"total": 100, "passed": 98, "failed": 1, "limited": 1, "seconds": 51.7}`**

对上一轮（t9：97 通过 / 2 失败 / 1 受限）的变化：

### 2.1 A6「裸数字不是会话引用」——**已转绿** ✓

```
[PASS] A6 / 目标的校验：裸数字不是会话引用
  error: "AgentError: conversation_ref_ambiguous: 裸数字不能当会话：不知道是哪个平台、私聊还是群聊。写成 qq:private:<号> 这样"
```

代码依据：`life/companion_delivery.py:queue()` 入口已接 `normalize_route()`（t13 D2）。同一场景的其余四条也都通过
（逐目标回执、不跨目标镜像、按 turn 汇总可追溯）。

### 2.2 B3「嵌套上限 2」——这一行仍红，但**红的理由变了**

```
[FAIL] B3 / 嵌套上限 2 真实生效：深处第 3 层被拒（nesting_limit 或父已结束）
  error: "AgentError: parent_ambiguous: 这个名字（companion-project_9c36…）同时挂着 2 件没结束的子 agent，
          按名字算深度会算不清；请改用 create 回执里的 agent_id 作为 parent_task_ref。"
```

**怎么判**：t9 那行判定用的探针是 `parent_task_ref = parent['task_ref']`，而 `task_ref` 是**项目名**，
同一项目下多件子 agent 会共用它。修复后 `_depth()` 对「按名字派且同名有多件未结束」的处理是
**明确拒绝并让你改用 `agent_id`**——这是正确行为，不是越层放行。所以这一行「红」是**探针写法**的问题，
不是上限失效。为了把上限本身验清楚，我另写了一个按 `agent_id` 逐层派的等价探针
（`tools/stage-c/verify_b3_exact.py`，证据 `b3-result.json`）：

```
[PASS] 按 agent_id 逐层派：0 → 1 → 2 都落得下，深度如实记账        {"depths": [0, 1, 2]}
[PASS] 第 3 层被 nesting_limit 明确拒绝（不是悄悄放行）            {"error": "AgentError: nesting_limit: 嵌套最多 2 层（再往下就是第 4 层）…"}
[PASS] 按项目名派且有同名兄弟时：明确 parent_ambiguous（不猜、也不算越层）
[PASS] 唯一父时按项目名派仍然可用（没误伤）                        {"depth": 1}
```

**结论**：嵌套上限 2 **真实生效**（0/1/2 落得下，第 3 层被 `nesting_limit` 拒）；
`parent_ambiguous` 是合理的新守卫。t9 那条探针的写法错误仍在脚本里（判定条件只认
`nesting_limit|parent_finished`），我**没有替实现者改判定**。

> **裁定（captain，2026-11-17）：B3 那行是「探针写法问题」，不是产品缺陷，也不是回归。**
> `tools/stage-c` 的判定条件**不改**。

### 2.2.1 给后来的人：B3 那行为什么不代表回归

阅读 `matrix-result.json` 时，B3 有一条 `ok=false`，**不要**把它当成嵌套上限失效或版本倒退。
按下面四步读一遍就能确认它是「正确的守卫在起作用」：

1. **看它报的是什么错**：`parent_ambiguous`，不是「放行了第 3 层」，也不是别的任何失败。
   报错原文就是守卫在解释自己：*「这个名字同时挂着 N 件没结束的子 agent，按名字算深度会算不清；
   请改用 create 回执里的 agent_id 作为 parent_task_ref。」*
2. **看探针怎么派的**：`t9` 那行用的是 `parent_task_ref = parent['task_ref']`，而 `task_ref` 是**项目名**
   （由 `_project_ref(project_key)` 派生），同一项目下多次 `create` 会**共用**它。拿一个共享的名字当父，
   本来就分不清「挂给哪一件」。
3. **看修复前的行为**：`_depth()` 那时按 `task_ref` 取 `ORDER BY created DESC LIMIT 1`，
   取到的是同层兄弟或自己 → 深度永远停在 1 → 第 3 层永远拦不住（这才是 t9 抓到的真缺陷）。
   修复后它改成「按 `agent_id` 精确匹配；只有对不上才按名字找，且必须唯一」，于是同名多件时**明确拒绝**。
   同一个输入，**行为从「静默算错、放行越层」变成「拒绝并让你说清是哪一件」**——这是修好了的形态。
4. **看按 `agent_id` 派的等价探针**（`verify_b3_exact.py` / `b3-result.json`，4/4 通过）：
   `depths=[0,1,2]` 合法、第 3 层 `nesting_limit`、唯一父按名派仍可用。**上限本身有效**。

一句话记法：**t9 那条 B3 行现在检验的是「共享的项目名能不能当父」——答案是「不能，请用 agent_id」，
这条判断是对的**；它早已不检验「嵌套上限是否生效」了，后者由 `b3-result.json` 负责。

这条探针要不要改成按 `agent_id` 派，属评审/工具维护的事，**不在产品修复范围**；
改了以后 B3 那一行会自然变绿，届时请同时保留一条 `parent_ambiguous` 的正向断言，
以免把「守卫在工作」这件事从证据里弄丢。


### 2.3 其余 98 条

无新失败。唯一的 1 条「受限」仍是 A5「转述措辞依赖真实模型」——假模型只能验到「能不能找回、哪一段能找回」，
**不冒充通过**。跨会话检索入口属 C 阶段，本轮**没有**把它算成失败。

## 3. 必做 2：阶段 B 执行入口的端到端（F2 是否真修好）

跑的是 `tools/stage-c/verify_repair_e2e.py`：假模型返回一段 `actions=[{kind:'agent', op:'create', …}]` 的决定
→ `Companion._run_cognition` 校验 → `_commit` 落待办 → 再跑**真正的** `Companion._workers()`。
全程不直接调台账函数（拿 `dispatch`/`create` 包了记录器来证明到底经过没经过）。

**结果：`{"total": 20, "passed": 18, "failed": 2}`**

| 判定 | 项 | 证据 |
|---|---|---|
| 通过 | F2-a 模型侧动作目录里有 `kind=agent` | `DECISION.actions.kind` 枚举含 `agent` |
| 通过 | F2-b 决策入口受理了模型侧 action | `companion_actions` 落了一行 `pending`，`source=evt_…` |
| 通过 | F2-c 待办里的动作就是模型给的那个 agent 动作 | `kind=agent, op=create` |
| **失败** | **F2-d 模型派的 agent 活真正进台账了吗** | `action_status=pending`、`ledger rows=[]`、`worker_would_pick_it=false`、`dispatch/create` 计数 0 |
| 通过 | F2-e「决策流」这条路上台账入口一次都没被调用 | `calls={dispatch:0, create:0}`——不是幂等，是根本没走到 |
| 通过 | **F2-f `executor.run(kind=agent, op=create)` 真的分派到台账并返回 queued** | `status=queued, agent_id=companion-agent_5cf0…, task_ref=…, operation_id=…`，台账从 0 → 1，`dispatch=1 create=1` |
| 通过 | F2-g 同一条链路读得回同一件（`op=inspect`） | 同一个 `agent_id`，`status=queued` |
| 通过 | F2-h 任务主线（TaskService）有记录 | `meta.action_task:*` 里查到该项目 |
| 通过 | F2-i 反证：执行器没接台账时明说「没发生」 | `status=failed, code=agent_not_wired, output=「…这次没有发生任何事。」` |
| **失败** | **F2-j 真后台循环把模型新派的 agent 活送进台账（基线差集）** | `baseline=1, fresh=[]`、`action_status=pending` |

### 3.1 失败定位（附代码行）

「模型侧动作提交 → 队列里出现任务」这条路**断在最后一段**：

1. `life/companion.py:2232` 把每个动作写进 `companion_actions`，状态 `pending`——**这一步是好的**（F2-b/c 证明）。
2. `life/companion.py:2808-2810` 的普通领活 SQL **显式排除** agent：

   ```sql
   ... AND COALESCE(json_extract(a.action,'$.kind'),'')!='agent' ORDER BY a.created LIMIT 1
   ```

   设计上说 agent 的活由台账执行器领，所以排除是对的。
3. `life/companion.py:2753-2767` 的台账执行器 `_agent_worker_one()` 只从 `companion_agent_tasks` 里取 `queued`
   ——**它不会去读 `companion_actions` 里的 pending agent 行**。
4. 于是「待办里的 agent 动作」和「台账」之间**没有任何一处在转移**：动作停在 `pending`，
   台账永远空。我另做了带打点的复现（`repro_f2_worker2.py`）确认：`_workers()` 跑满 4 秒，
   `ActionExecutor.run` 一次都没被调用，台账为空，`companion_actions` 仍是 `pending`。

**这不是「代码没写」**：`executor.run` 的 agent 分支（`life/companion_executor.py:153-166`）确实修好了，
直接调它能进台账（F2-f）。缺的是「谁把 pending 的 agent 动作交给它」这一步。
t13 自检之所以能过，是因为自检**直接调** `executor.run(kind='agent')`/`manager.dispatch`，绕过了这一段。

**生产佐证**（只读 SQL，`mode=ro`）：生产 `companion_actions` 最近 12 条里没有任何 `kind=agent` 行，
`companion_agent_tasks` 与 `companion_agent_events` 都是 **0 行**——也就是说，上线以来模型还没派过子 agent，
这一环在生产上同样未经真实流量验证。

**建议修法**（给实现者，本轮未改产品代码）：在 `_commit` 落 pending 之后、或 `_workers()` 循环里，
对 `kind='agent'` 的 pending 行做一次「认领 → `executor.run(action)` → 记回执」，即
把这类行交给执行器的 agent 分支（与 town/body 走隐藏执行器同一种做法），而不是丢给排除 agent 的普通领活 SQL。
抓一小段时间窗口内 `pending` 且 `kind=agent` 的行，调 `self.executor.run(action)`，再按既有 `_settle` 记回执即可。

## 4. 必做 3：预算三模式（真配置对象）

| 判定 | 模式 | 证据 |
|---|---|---|
| 通过 | `off` | `status=queued`，`mode=off` |
| 通过 | `bounded_estimate` | `status=queued`，`mode=bounded_estimate`，`enforceable={concurrency:true,runtime_s:true,calls:false}`、`missing_limits=["calls"]` |
| 通过 | `strict` | `status=rejected`、`needs_setup=true`、原因：「strict 需要每一次调用的完整逐次用量；这个后端不报用量…不会替你改成 strict 以外的模式。」 |
| 通过 | `enabled=true` 无 `mode` | 仍按 **strict** 处理（`rejected`），**不静默降级** |
| 通过 | 未知用量不写 0 | `accounting.unknown_written_as_zero=false`、`usage_known=false` |
| 通过 | 记账层 | `merge_usage(None,None) is None`；一边有数 → 缺字段 `None` 且 `unknown=true` |
| 通过 | 口径一致 | `awareness.spending_mode` 与 `agents.budget_mode` 对同一策略都给 `strict` |

用的是真 `SpendingPolicy` 对象（`SimpleNamespace(enabled=…, mode=…)` 注入到 `core.spending.policy`），
不是绕过配置直接调判定函数：`create` 读的是真实策略。

## 5. 必做 4：生产核对与残留

采样窗口：`2026-11-17T16:03:40+08:00` → `2026-11-17T16:04:51+08:00`（matrix 51.7 s + e2e 18.6 s）。

**`config.toml` sha256 前后一致**：

```
f5e169264efad3563a65ce4f3bccc20d52afee341e57fd6b6c31ca9ff51addb2  (before)
f5e169264efad3563a65ce4f3bccc20d52afee341e57fd6b6c31ca9ff51addb2  (after)
```

**生产库 stat 前后**：

| 文件 | before | after |
|---|---|---|
| `life.db` | 370343936 / mtime 1794902593 | 371355648 / mtime 1794902687 |
| `life.db-wal` | 9022832 / 1794902619 | 9022832 / 1794902691 |
| `life.db-shm` | 32768 / 1794902601 | 32768 / 1794902687 |

**这些变化不是验收写的**，依据：验收全程只用 `sqlite3 mode=ro`（matrix/e2e 都在 `/tmp` 的全新空库上跑，
没打开生产库读数据）；变化发生在生产服务**自己**工作期间——同一窗口内 `life.db` 长了约 1 MB、
`-wal`/`-shm` 的 mtime 一起前移，而 `config.toml` 分毫未动。判定口径因此拆成两条：
**「验收没写生产库」= 验收代码里没有任何对 `/var/lib/companion` 的写路径（可复核脚本）**；
**「服务自己在写」= 上面的 stat 变化**。两者不能混为一谈。

**服务状态前后完全一致**（`service-before.txt` / `service-after.txt`）：

```
ActiveState=active  SubState=running  MainPID=864221  NRestarts=0
ActiveEnterTimestamp=Tue 2026-11-17 15:24:13 CST   ExecMainStartTimestamp=Tue 2026-11-17 15:24:13 CST
```

**`/tmp` 无残留 + 清理回执**：`residue-after-clean.txt` 为空；收尾执行
`tools/stage-c/p8_final_clean.sh`：

```
removed: /tmp/p8b3-20261117-160557 after 1 pass(es)
removed: /tmp/p8e2e-20261117-1545 … /tmp/p8e2e-20261117-1608 (5 个)
removed: /tmp/p8repro-f2 after 1 pass(es)
removed: /tmp/p8repro-f2b after 1 pass(es)
=== residue after cleanup (/tmp/p8*, /tmp/p3*)  none
=== integration/ipc leftovers                    none
```

## 6. 本轮自己出的一次事故（如实记录）

第一次跑收集脚本时，我把归档目录 `.../.acceptance/p8-…-1550` 当成了临时根传给验收脚本，
而脚本要 `copytree` 生产树——于是**归档目录被拷进了自己的副本里**，形成递归目录（路径越滚越长，
报 `File name too long`）。处理：

1. 立刻停掉该进程（`pkill`），确认无残留进程；
2. 用 `tools/stage-c/clean_nested.sh` 由深到浅删掉那个目录（已确认删除，`removed … after 1 pass(es)`）；
3. **改脚本加硬保护**：`_guard_root()` 在开跑前拒绝「临时根落在生产树里」或「临时根在 `.acceptance` 里」，
   `stage_tree()` 的跳过名单补上 `.acceptance`、`.review-server`、`.srv-life*`；
4. 重新按「工作区在 `/tmp`、归档在 `.acceptance`」跑了一遍（就是本轮证据）。

影响：只污染了那个作废的归档目录，**没有触及生产代码、生产数据、服务状态**（服务状态前后一致可证）。
脚本缺陷已修，复跑不会再犯。

## 7. 待下游处理

| # | 事项 | 证据 |
|---|---|---|
| 1 | **F2 最后一环（产品缺陷，待修）**：pending 的 `kind=agent` 动作没有任何一处交给执行器（§3.1，`companion.py:2808-2810` 与 `2753-2767` 之间缺转移） | `e2e-result.json` F2-d / F2-j；`repro_f2_worker2.py` 打点 |
| 2 | **不是产品缺陷**：t9 那条 B3 探针用项目名当父，遇到同名多件时命中新守卫 `parent_ambiguous`。captain 已裁定为「探针写法问题」，`tools/stage-c` 判定条件不改；上限本身由 `b3-result.json` 4/4 证明有效 | `matrix-result.json` B3；`b3-result.json`；§2.2.1 |
| 3 | 真实 DSH 子 agent 与真实模型措辞判断仍未跑（要花钱 / 需要真模型），A5 保持「受限」 | `matrix-result.json` limited 行 |

## 8. 证据路径（可回收）

云端 `/opt/qq-deepseek/.acceptance/p8-20261117-1610/`，本地 `docs/evidence/p8-20261117-1610/`（同一份）：

| 文件 | 内容 |
|---|---|
| `matrix-result.json` / `matrix.log` | 100 条判定与逐条日志（98/1/1，51.7 s） |
| `e2e-result.json` / `e2e.log` | F2 端到端 10 条 + 预算 10 条（18/2，18.6 s） |
| `b3-result.json` | 按 `agent_id` 逐层派的嵌套上限正反例（4/4） |
| `prod-before.txt` / `prod-after.txt` | 生产库与配置的跑前 / 跑后 stat |
| `prod-config-sha-before.txt` / `-after.txt` | `config.toml` 前后 sha256 |
| `service-before.txt` / `service-after.txt` | 服务状态（active / NRestarts=0 / 15:24:13） |
| `residue-after-clean.txt` | `/tmp` 残留清单（空 = 无残留） |
| `time-before.txt` / `time-after.txt` | 采样时刻 |
| `verify_awareness_agents.py`、`verify_repair_e2e.py`、`verify_b3_exact.py`、`p8_collect.sh`、`deep_clean.sh`、`show_rows.py` | 可直接复跑的脚本 |

常驻脚本在 `/opt/qq-deepseek/tools/stage-c/` 与本地 `tools/stage-c/`。复跑：

```sh
sh /opt/qq-deepseek/tools/stage-c/p8_collect.sh /opt/qq-deepseek/.acceptance/p8-$(date +%Y%m%d-%H%M%S)
sh /opt/qq-deepseek/tools/stage-c/b3_run.sh      # 嵌套上限正反例
```
