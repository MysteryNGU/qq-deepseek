# 阶段 B 修复轮（P6 / t13）：接通执行入口、预算三模式、dsh 守卫、路由与嵌套、重启上线

本文是 `docs/companion-agents-stage-b.md` 的修订版。t7 交付后被评审（t9/t10）指出七项问题，
本轮按用户「全修」授权逐条处理；F2/F3/F4/D2 修完并**重启上线**，F1/F7 的证据链补齐，F8（行尾）
在部署时核对。改动发生在云端 `/opt/qq-deepseek`，并逐字节回写本地源码副本。

**云端证据目录**：`/opt/qq-deepseek/.acceptance/20261117-144013-repai/`
（含可复跑脚本、before/after sha256、属主与行尾、运行日志、重启前后状态、回滚包
`backup_final/life-pre-restart.tar.gz` 与 `restart/evidence.tar.gz`）。
本地只读副本：`docs/evidence/t13-20261117-144013-repai/`（可随时重取）。

## 0. 上线状态（本轮最重要的一条）

| 项 | 值 |
|---|---|
| 重启前 | `ActiveEnterTimestamp=Tue 2026-11-17 09:08:16 CST`，`MainPID=699927`，`NRestarts=0` |
| 重启后 | `ActiveEnterTimestamp=Tue 2026-11-17 15:24:13 CST`，`MainPID=864221`，`NRestarts=0`，`is-active=active` |
| 重启后日志 | 自 `15:24:13` 起 journal **0 行**（无 Traceback、无 ERROR/CRITICAL） |
| 邻居服务 | `companion-embedding=active`、`nginx=active`、`napcat=inactive`（无 napcat 容器，未被误伤） |
| `/health` | 无 token 返回 `401 Unauthorized`（鉴权正常）；`/health` 本身不经 token 也活着说明端口在监听 |
| 生产库表 | 只读查询确认 `companion_agent_tasks`、`companion_agent_events` 两张表已存在，当前 0 行（无人派活） |
| 生产 `config.toml` | sha256 `f5e16926…`、mtime `2026-11-16 12:06:24` —— **部署没有改动它** |
| 生产 `life.db` | mtime 被服务自身写入推进（WAL 写入是常态），部署本身不写库（全部验收都在临时 HOME 的副本上） |

## 1. 改了哪些文件（云端=本地，sha256 一致）

| 文件 | sha256（前 16） | 行尾 | 改动 |
|---|---|---|---|
| `life/companion.py` | `857143b7998d575b` | LF | `executor.attach_agents(self.agents)`；恢复 dsh 预算守卫；投递登记改走 `_queue_delivery()`（单条失败不拖垮整轮） |
| `life/companion_agents.py` | `853bb29e8b85ae3a` | LF | `decide()` 契约 §11 结构 + `budget_mode()`/`budget_decision()`；`create` 读真实策略；`_depth()` 按 id 精确取父、退回按名且**必须唯一**；`_chain_depth()` 沿 `parent_id` 链数层；`dispatch()` 在无 op/终态时给 `op_required`/`agent_not_runnable` |
| `life/companion_awareness.py` | `964b7ee55527a07` | LF | `spending_mode()` 转调 `companion_agents.budget_mode`；修 `_agent_tasks` 里 `sqlite3.Row.get()` 的崩溃（生产库有 `companion_activity` 行时 awareness 段会静默降级） |
| `life/companion_executor.py` | `6f99046c44e3bfde` | CRLF（392 行，原样） | `kind=='agent'` 分派到 `AgentManager.dispatch`；`attach_agents()`/`_agents_manager()`；未接台账回 `agent_not_wired` |
| `life/companion_delivery.py` | `1408fa52b031dd20` | CRLF（284 行，原样） | `queue()` 入口 `_route()` 校验并规范化，裸数字抛 `conversation_ref_ambiguous` |
| `tests/test_agent_repair.py`（新） | `02da2dd7abcf7909` | LF | 22 条离线回归（F2/F3/F4/D2） |
| `tools/stage-b/` | — | — | 本轮与 t7 的全部可复跑脚本（云端已同步） |

F8 核对：`life/companion_executor.py`、`life/companion_delivery.py` 原来是 CRLF，本轮改动**保持
CRLF**；`companion.py`/`companion_agents.py`/`companion_awareness.py` 是 LF，保持 LF。
全部以 `install -o companion -g companion -m 644` 落盘，属主是 `companion:companion`（不是 root）。

## 2. F2（blocker）：执行入口已接通

之前 `AgentManager.dispatch()` 全仓零调用点：`_worker()` 的 SQL 用
`AND COALESCE(json_extract(a.action,'$.kind'),'')!='agent'` 排除 `kind='agent'`，而
`_agent_worker_one()` 只被 `_workers()` 调用——模型在线上根本 create 不了子 agent。

现在这条链是通的：

```
模型 actions[] 里 kind='agent'
  → companion_actions(pending)
  → _worker() 领到 → executor.run(action)
  → ActionExecutor.run() 的 kind=='agent' 分支 → AgentManager.dispatch(action)
  → create/send/inspect/wait/cancel/resume/read_artifact
```

`ActionExecutor` 通过 `attach_agents()` 拿到同一份台账（服务模式下执行器链外面还包了
`RemoteExecutor`，所以只挂 `core.agents` 不够——这是本轮第一个真实缺陷）。

验收（`restart/residence_probe.json`，重启后跑的真 `Companion`）：
`executor_wired_to_ledger=true`、`agent_in_decision_enum=true`、`op=create` 返回
`status=queued` 且 `agent_id/task_ref/operation_id` 齐全、随后 `op=inspect` 读回同一件；
`_agent_worker_one()` 单步把排队活跑成 `completed`。

## 3. F3（high）：预算三模式在真实配置上可达

原实现两个缺陷：`_budget_mode()` 读 `SpendingPolicy.mode`（这个字段根本不存在）导致
`enabled=true` 一律 strict；`create` 硬编码 `usage_known=False` 导致开了预算连
`bounded_estimate` 都被拒。

现在 `decide()` 返回契约 §11 要求的字段（`mode/limits/missing_limits/enforceable/accounting/
usage_known/reason`），模式判定与裁决都收口在 `companion_agents.budget_mode/budget_decision`：

| 模式 | allowed | limits | missing_limits | enforceable.calls | accounting.mode |
|---|---|---|---|---|---|
| off | true | {} | [] | false | off |
| bounded_estimate | **true** | concurrency/runtime_s | 没定的（通常 calls） | **false** | known_or_unknown_never_zero |
| strict | false（needs_setup） | {} | ['usage'] | false | complete_per_call |

`enabled=true` 且无 `mode` → 仍是 **strict**（不静默改用户选择）；`bounded_estimate` 下 create
可执行；调用次数上限如实标 `enforceable=False`。

验收：`tests/test_agent_repair.py::BudgetModes`、`::BudgetOnRealConfig`（三种模式在真
`Companion` + 真 `SpendingPolicy` 上各 create 一次）以及重启后的
`restart/residence_probe.json` → `budget_modes`：off/bounded_estimate 都是 `queued`，
strict 是 `rejected + budget_strict_unmetered + 原因`；未知用量 `usage_known=false`，
`accounting.unknown_written_as_zero=false`。

> t16（P9）之后 `SpendingPolicy` 已加 `mode` 字段（`off/strict/bounded_estimate`），
> `config.toml` 里可以直接写 `mode`，一路传到真实判定点；**非法值 `parse()` 直接报错**
> （`invalid_spending_policy_mode`），不静默回落 strict。`_legacy_spending_mode` 已删除。

## 4. F4（high）：dsh 预算守卫回来了，且是真的判定

t7 把守卫删掉了——那不是补上计费。现在守卫回来了，但走的是**与子 agent 同一个**裁决函数：

```python
if action['kind']=='dsh' and self._dsh_budget_blocked():
    raise SessionFailure('budget_unmetered', self._dsh_budget_reason())
```

* strict：拒绝，原因写明「strict 需要每次调用的完整逐次用量，DSH 只暴露 4 个 RPC」；
  能力目录里 `action.dsh` 同时是 `needs_setup` 并带同一原因（不静默消失）。
* off / bounded_estimate：正常执行；`_settle` 照常记 `_record_usage`，未知用量走
  `spending.reported_cost()` 返回 `None` 的真实路径（进 `unknown_calls`），不写 0。

验收：`tests/test_agent_repair.py::DshGuard` 与 `restart/residence_probe.json` 的
`dsh_guard`：off `blocked=false`、strict `blocked=true` 且原因可读。

## 5. D2：投递路由与嵌套深度

1. **投递**：`companion_delivery.queue()` 入口先过 `_route()` → `normalize_route()`，
   裸数字抛 `conversation_ref_ambiguous`；空串（沿用默认投递）与 `all`（全端）不是具体会话，照旧允许。
2. **嵌套**：`_depth()` 优先按台账主键 `id` 精确取父（唯一）；`parent_task_ref` 给的是节点名
   （项目 key / task_ref）时，`_parent_by_name()` **只认唯一一件**，多件同名就
   `parent_ambiguous` 并提示改用 `create` 回执里的 `agent_id`。深度以 `parent_id` 链为准
   （`_chain_depth()`），记账里的 `depth` 字段只作展示、且结果不会小于 `父 depth + 1`。

验收：
* `tests/test_agent_repair.py::DeliveryRoute`、`::NestingDepth`（父子孙 0/1/2，第 4 层
  `nesting_limit`；同名多件拒绝而不是猜；名字唯一时仍可挂父）。
* t9 同一脚本 `tools/stage-c/verify_awareness_agents.py` 复跑（`repro_t9/run.log`）：
  **A6「目标的校验：裸数字不是会话引用」转绿**（错误码 `conversation_ref_ambiguous`）。
* B3「嵌套上限 2 真实生效」**仍是红**，但失败原因变了：现在返回的是
  `parent_ambiguous`——t9 的探针把 `parent['task_ref']` 当父传进去，而在它自己的前置步骤里
  同一会话已有别的子 agent 共用这条 task_ref。**这正是本轮要修的缺陷**（按名字取父会取到
  兄弟/自己），修法就是拒绝并要求用 agent_id；脚本的判定条件只认
  `nesting_limit|parent_finished`，所以它仍然判红。用 agent_id 的等价场景
  （`repro_t9/b3_exact.txt`、单元测试）显示层级 0/1/2 正确、再往下一层被 `nesting_limit` 拒绝。

## 6. F1 / F7：版本与证据链

* `before.sha256`（修复前）：`companion.py f75aa634…`、`companion_agents.py 04e5d267…`、
  `companion_awareness.py cd3674cb…`、`companion_executor.py 75157f44…`、
  `companion_delivery.py cbe1289d…` —— 与评审记录一致。
* `after.sha256` / `after.stat` / `after.eol`：修复后的 sha256、属主（`companion:companion`）、
  行尾。
* `restart/before.txt|after.txt`、`restart/*.show.txt`：重启前后的 `ActiveEnterTimestamp`、
  `MainPID`、`NRestarts`、`InvocationID`。
* `restart/journal.before.txt|journal.final.txt`：重启前后 journal。
* `restart/residence_probe.json`：重启后真 `Companion` 自检结果。
* `restart/prod-agent-tables.json`：生产库里两张台账表已建（只读查询）。
* `backup_final/life-pre-restart.tar.gz`：重启前完整备份，可直接回滚。
* `restart/evidence.tar.gz`：整个验收目录的打包。

## 7. 仍不成立 / 未做（如实列）

1. **B3 那一行在 t9 脚本里仍是红**：脚本用 `task_ref` 当父名，而此刻同名有多件 → 现在按设计
   拒绝（`parent_ambiguous`）。这是「不再猜」的结果，不是层级没生效；用 `agent_id` 的等价
   场景是对的。要把它变绿需要改 t9 脚本让探针改用 `agent_id`——属评审侧改动，本轮没动
   `tools/stage-c/` 的判定条件。
2. **真实 DSH 子 agent 仍未跑**：`node=dsh` 只做了编译与代码审查（要花真实模型的钱）。
3. ~~**`SpendingPolicy` 仍不认识 `mode`**~~ —— t16 已修：dataclass 带 `mode`，非法值报错；
   需要改 `spending.py` 的 dataclass（建议下一轮）。
4. **`send` 的 stdin**（t16 订正）：`stub`/`command`/`dsh` 后端都不读 stdin。回执现在一律是
   `applied='stored'`、`delivered=false`、`applied_reason='worker_does_not_read_stdin'`，文案写
   「已入台账……但 worker 尚未读取」。只有 worker 真的声明读走（`live['reads_stdin']`）才可能
   是 `delivered`——目前没有任何后端这么做，所以线上 **永远** 是 stored。
5. **生产还没有真实派活**：台账表已建、0 行；`kind=agent` 的真实链路要等小鱼自己派一件才走得到
   （实例外无法替她派）。
6. `people.identity` 那条契约测试与子 agent 无关，本地仍失败（P4 既有问题）。


## 9. t16（P9）追加：H1 / H3 / 权限 / F2 最后一环

| 项 | 结论 | 证据 |
|---|---|---|
| H1 send 诚实 | 回执 `applied='stored'`、`delivered=false`、`applied_reason='worker_does_not_read_stdin'`；重复 message_id 同样 stored | `final-checks/hard_acceptance.json` → `h1`；`tests/test_agent_repair.py::SendHonesty` |
| H3 预算 mode | `SpendingPolicy.mode` 进 dataclass，`effective_mode()` 为唯一真源；非法值抛 `invalid_spending_policy_mode`；`_legacy_spending_mode` 删除 | `final-checks/hard_acceptance.json` → `h3`；`::SpendingModeConfig` |
| 部署权限 | `.acceptance/20261117-144013-repai/` 已由 root 改为 `companion:companion`（只改这一个目录）；产品文件用 `sudo -u companion install` 落地；`tests/` 目录本身保持 root，只把 `test_agent_repair.py` 一个文件改成 companion | `restart-t16/`、`final-manifest` |
| F2 最后一环 | `_workers()` 先调新的 `_agent_pending_one()`：认领 pending 的 `kind='agent'` 行动 → `executor.run()` → 既有 `_settle()` 记回执并回一次结果轮 | 见下 |

F2 硬验收（`final-checks/hard_acceptance.json` → `f2`）：从**模型决策入口**（`_commit`）提交
`kind=agent/op=create` 后，`companion_actions` 出现 `pending` 行；调用一次
`_agent_pending_one()` 后该行变 `queued`、`companion_agent_tasks` 从 0 行变 1 行
（`queued`，`inspect_status=queued`）、`companion_agent_events` 从 0 增到 2。
直接调 `executor.run` 的那条老测试不再作为判定依据。
