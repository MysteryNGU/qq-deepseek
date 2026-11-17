# P7 修复轮独立评审（t14）：对 t13 / P6 的逐条核对

评审人：reviewer（独立评审，只读；未改产品代码、未重启服务、未写生产库、未发 QQ）
时间：2026-11-17 15:33–16:05（Asia/Shanghai）
被评对象：t13「P6 修复轮：接通阶段 B 执行入口、修预算与路由/嵌套缺陷、冻结生产版本证据」
证据来源：云端 `/opt/qq-deepseek/.acceptance/20261117-144013-repai/`（只读 ssh 取）、生产只读 SQLite、
本地源码副本 `C:\Users\76328\qq-deepseek\life`（与云端逐字节一致，见 §2）。

---

## 0. 结论（一句话）

修复轮的**核心四项（F2 执行入口、F3 预算三模式、F4 dsh 守卫、D2 路由与嵌套）在代码与隔离环境上
真实成立，并且已经重启上线（ActiveEnterTimestamp 15:24:13）**；但本轮又重犯上一轮的证据病：
**部署后同一个文件被再改一次（companion_awareness.py 15:23:39 → 实际文件 964b7ee5…，而 after.sha256
记的是 15a0f024…）、部署脚本在 root 下执行导致 config.toml 与证据目录属主变 root、生产库被服务自己
写入后又在验收热路径上被只读副本读取**。F5（send 标 delivered 但 worker 不读 stdin）**未修**且本轮
报告 §7.4 的描述与实测不符。

判定：**needs_revision**（不阻断"上线事实"，阻断"证据链可复现"与"F5 不再谎报"）。

---

## 1. 五档证据（分开列，不合并）

| 档 | 阶段 A（认知层） | 阶段 B（子 agent） |
|---|---|---|
| ① 代码已实现 | ✔ 云端=本地逐字节一致 | ✔ 同上 |
| ② 离线测试通过 | ~ 阶段 A 的 25 条单测在云端 `tools/stage-a/` 已补回；本轮未重跑 | ~ 22 条修补回归在云端 **22/22 OK**（`run_repair.log`）；本地因精简检出（缺 `life/companion_model.py`、`jsonschema` 在 `.test-deps`）15 条 ERROR，属环境差异，非产品缺陷 |
| ③ 在线接通（运行进程里真的走得到） | ✔ 生产进程 15:24:13 起持续认知；`meta.companion:xiaoyu:read_backend` 在 15:39:58 被写，证明 awareness 段在线上每轮都被装配 | **~** 链在代码里通了（executor `kind=='agent'` → `dispatch` → 台账），真实 Companion 自检能 create/inspect/单步跑完；但**生产台账 0 行**，还没有一次"她自己派活"的线上记录 |
| ④ 真机验收（真实 DSH 子 agent / 真实 QQ） | ✘ 未做 | ✘ 未做（`node=dsh` 只做编译与代码审查，未花真实模型费用） |
| ⑤ 实际发布 | ✔ 已重启上线（用户已确认）；但**证据目录与 after.sha256 不能复现当前字节** | 同左 |

> 按任务口径：③ 对阶段 B 是"已重启、入口已通"，但仍缺真实派活证据；④ 一律未做，报告里不得暗示已验收。

---

## 2. 版本与一致性（独立复核，不引用报告数字）

云端 `life/` 实际 sha256（本次只读取样）：

```
companion.py              857143b7998d575b…  3007 行  CRLF 0     mtime 15:24:06
companion_agents.py       853bb29e8b85ae3a…  2110 行  CRLF 0     mtime 15:17:30
companion_awareness.py    964b7ee55527a079…   860 行  CRLF 0     mtime 15:23:39
companion_executor.py     6f99046c44e3bfde…   392 行  CRLF 392   mtime 15:24:06
companion_delivery.py     1408fa52b031dd20…   284 行  CRLF 284   mtime 14:40:13
companion_context.py      fa92ffd319b72fe1…    57 行  CRLF 57    mtime 09:08:00（本轮未动）
spending.py               f6626411283d72b5…               CRLF 0（本轮未动）
```

- 本地 `life/` 同 8 个文件 md5 与云端**完全一致**；`companion_executor.py`/`companion_delivery.py`
  本来就是 CRLF，本轮**保持 CRLF**（392/284 行），其余保持 LF——F8 的行尾问题本轮没有扩大。
- 五个文件 `ast.parse` 全部 OK；属主 `companion:companion`（`ls -la` 与 `owner.final.txt` 一致）。

---

## 3. 逐条核对 t13 的五项承诺

### 3.1 F2 执行入口——**已修，链在代码与隔离环境上真实成立**

独立复核到的调用链（不是"函数存在"）：

1. `life/companion.py:452-453` 构造期 `executor.attach_agents(self.agents)`；
2. `life/companion_executor.py:77-88` `attach_agents()/ _agents_manager()`：优先用挂进来的台账，
   否则从角色上取——这正是服务模式（`RemoteExecutor` 外包）必需的一步；
3. `life/companion_executor.py:153-167` `kind=='agent'` → `await manager.dispatch(action)`，
   未接台账时返回 `agent_not_wired` 并明说"没发生"；
4. `life/companion.py:2807-2810` `_worker()` 的 SQL 仍排除 `kind='agent'`（避免两处抢同一行），
   子 agent 只由 `_agent_worker_one()` 领；
5. `life/companion_agents.py` `dispatch()` → 七操作（`OPS`）。

云端实测（我的只读重跑，非引用报告）：
- 真实 `A纪律打点`——见 §4 `send` 之外的 create/execute 复核：`create` 返回 `queued`，
  `execute` 返回 `completed`、`usage=None`，**没有向 `companion_usage` 写任何 0**；
- 生产库 `/var/lib/companion/xiaoyu/data/life.db` 里 `companion_agent_tasks`、`companion_agent_events`
  **两张表已存在、0 行**；`companion_actions` 里 `kind='agent'` 的行动 0 条。

**判定：代码与离线成立；线上"她真的派过一件"仍未发生（0 行），第 ③ 档对阶段 B 只算"入口已通"。**

### 3.2 F3 预算三模式——**已修，真实配置对象上可复现**

我在云端用**真 `Companion` 里同一个 `AgentManager` + 真 `SpendingPolicy`/带 `mode` 的策略对象**
重跑（不引用 `residence_probe.json` 的数字）：

| 策略 | create 结果 | mode | accounting | usage_known | unknown_written_as_zero |
|---|---|---|---|---|---|
| `enabled=false` | `queued` | off | off | false | false |
| `enabled=true, mode=bounded_estimate` | **`queued`** | bounded_estimate | known_or_unknown_never_zero | false | false |
| `enabled=true, mode=strict` | `rejected / budget_strict_unmetered` + 可读原因 | strict | complete_per_call | false | false |
| `enabled=true`（无 mode） | `rejected / budget_strict_unmetered` | **strict** | complete_per_call | false | false |

- `enabled=true` 无 `mode` 仍是 strict——符合"不静默改用户选择"。
- `budget_mode()` 只读 `enabled`/`mode`，不再假设 `SpendingPolicy` 有那个字段；`create()` 读真实策略
  （`_spending_policy()` → `core.spending.policy`），模式收口到 `budget_mode/budget_decision`。
- 未知用量：`merge_usage(None, None) is None`、`accounting.unknown_written_as_zero=false`，
  实测执行完一件后 `companion_usage` **没有任何写入**（不是写 0）。

**一个如实保留的缺口**：`config.toml` 里仍不能直接写 `mode`（`SpendingPolicy.parse()` 拒绝未知字段），
所以 `bounded_estimate` 目前只能由部署侧把 `mode` 挂到策略对象、或由动作里 `budget.mode` 显式传。
这使"用户在配置里选三模式"仍不可用（见 H3）。

### 3.3 F4 dsh 守卫——**是恢复语义，不是继续删代码**

- `life/companion.py:2831-2832` 恢复了派活前守卫：
  `if action['kind']=='dsh' and self._dsh_budget_blocked(): raise SessionFailure('budget_unmetered', self._dsh_budget_reason())`；
- 守卫与子 agent 共用同一裁决（`dsh_budget_verdict()` → `companion_agents.budget_decision(provider='dsh')`）。
- 我的只读重跑：off `blocked=false` / `enabled 无 mode`（strict）`blocked=true` 且原因写明
  "DSH 只暴露 4 个 RPC、没有逐次 usage" / `bounded_estimate` `blocked=false`。
- 能力目录：strict 下 `action.dsh needs_setup` 带原因（t9 复跑日志 B6 两条 PASS 与我读到的
  `companion_awareness.capabilities()` 分支一致——`needs_setup` 缺 reason 会直接抛错，构造上防止静默）。

**判定：成立。**

### 3.4 D2 路由与嵌套——**两条都成立**

- 路由：`life/companion_delivery.py:_route()` → `companion_agents.normalize_route()`。
  我的只读重跑：`normalize_route(conversation_ref="10001")` → `conversation_ref_ambiguous`；
  `delivery.queue(..., conversation_ref="10001")` 在**触到数据库之前**就抛
  `AgentError: conversation_ref_ambiguous`（用会主动报错的假 repo 验证）。
- 嵌套：`life/companion_agents.py:1048-1116` `_depth()` 先按台账主键 `id` 精确取父，
  对不上才 `_parent_by_name()`，**只认唯一**，多件同名 → `parent_ambiguous`；
  深度沿 `parent_id` 链（`_chain_depth()`），不靠自报 `depth`。
  我的只读重跑：父子孙 depth 0/1/2，第 4 层 → `nesting_limit`；
  同一 task_ref 被 3 件共用时按名字挂父 → `parent_ambiguous` 并提示改用 `agent_id`。

**t9 脚本 B3 仍红的原因成立**：探针用 `parent['task_ref']` 当父，而那正是"名字不唯一"的场景，
现在是**按设计拒绝**而不是猜；用 `agent_id` 的等价场景 0/1/2 正确、第 4 层被拒。
本轮没改 `tools/stage-c/` 的判定条件——但这也意味着 **t9 的 100 条里仍有 1 条红、1 条 limited**，
不能把"98/100"说成"验收全绿"（见 M2）。

### 3.5 证据链——**部分不成立（本轮主要问题）**

成立的部分：`before.sha256`（8 个文件）与上一轮评审记录的上线版本 sha256 **逐一吻合**
（`companion.py f75aa634…`、`agents 04e5d267…`、`awareness cd3674cb…`、`executor 75157f44…`、
`delivery cbe1289d…`），说明"改前基线"这一环终于有据可查；`restart/` 目录含重启前后
`ActiveEnterTimestamp/MainPID/NRestarts/InvocationID`、journal、`/health`、`/health` 后探测、
邻居服务、回滚包 `backup_final/life-pre-restart.tar.gz` 与整目录 `evidence.tar.gz`；
`tools/stage-a/` 也已在本轮补回 `test_companion_awareness.py`（F7 阶段 A 部分已修）。
`tools/stage-b/` 现在云端存在（16 个文件，含 `t13_*.sh`）。

不成立的部分见 §5 的 B1（属主）与 B2（after.sha256 对不上线上字节）。

---

## 4. 我独立跑过的复核（命令与结果）

云端（只读，`/opt/qq-deepseek` 内用 `.venv-companion` 解释器跑一次性 stdin 脚本，不落盘）：

| 复核 | 结果 |
|---|---|
| `normalize_route("10001")` | `AgentError conversation_ref_ambiguous` |
| `delivery.queue(..., conversation_ref="10001")`（假 repo，触库即报错） | 在触库前抛 `conversation_ref_ambiguous` |
| 三层嵌套 `create(parent_task_ref=<agent_id>)` | depth 0/1/2；第 4 层 `nesting_limit` |
| 同名 task_ref 挂父 | `parent_ambiguous`（提示改用 agent_id） |
| 真 `AgentManager` + 真 `SpendingPolicy` 三种模式 create | off/bounded → `queued`；strict 与"enabled 无 mode" → `rejected + 原因` |
| `budget_decision(policy, provider='dsh')` | off false / strict true+原因 / bounded false |
| `execute(node='echo')` | `completed`，`usage=None`，`companion_usage` **零写入** |
| `send`（模拟子 agent 正在跑、stdin 可写） | 回执 `delivered=True, applied='delivered', "已送达正在运行的子 agent。"`；随后检查 `life/companion_agent_worker.py` 源码：**不含 `stdin`、不读输入** |
| 云端 5 个文件 `ast.parse` | 全部 OK |
| 生产库只读 | 两张台账表存在、0 行；`kind='agent'` 行动 0 条；`read_backend` 15:39:58 已写 |

本地：
- `python -B -X utf8 -m unittest tests.test_agent_contract` → 25 条 23 过（2 条既有失败，与 B 无关）；
- `tests.test_agent_repair` 本地 15 条 ERROR（缺 `life/companion_model.py` 与 `jsonschema`），
  属本地精简检出，云端 22/22 OK；两者都记录在案，不当作回归。

---

## 5. Finding（按严重度）

### blocker

**B1：生产 config.toml 与 .acceptance 目录属主变成 root（评审只读发现，需实现方修复）**
- 文件：`/var/lib/companion/xiaoyu/config.toml`、`/opt/qq-deepseek/.acceptance/`
- 现象：`ls -la` 显示 `config.toml` 属主 `root:root`（14660 B，mtime 11-16 12:06:24 未变、sha256
  `f5e16926…` 未变）；`.acceptance/20261117-144013-repai/` 属主 `root:root`。上一轮评审时
  `config.toml` 还是 `companion:companion`。说明本轮部署脚本以 root 跑过（`install`/重定向/`chown` 之外的动作）。
- 为什么算 blocker：`companion` 进程以 `User=companion` 运行，写不了 `root:root` 的配置备份/回滚目录；
  更要紧的是"部署过程改动了生产 HOME 里的文件元数据"这件事没有被报告，违反"部署不碰
  `/var/lib/companion/**`"的既有约定（t4 `docs/cloud-sync-policy.md`）。
- 需要修：`chown companion:companion /var/lib/companion/xiaoyu/config.toml`；把 `.acceptance` 的证据
  目录属主也归到 companion（或明确"证据目录只读、由 root 维护"并写进策略）；部署脚本改为
  `sudo -u companion install ...` 或部署后统一 `chown -R`，并在证据里附 `ls -la` 前后对比。

**B2（本轮新增）：部署后同一个文件被再改一次，after.sha256 不能复现当前字节**
- 文件：`life/companion_awareness.py`
- 现象：`after.sha256` 记的是 `15a0f024…`（059 行），`backup_final/companion_awareness.py` 也是
  `15a0f024…`；而云端**实际**是 `964b7ee5…`（860 行，mtime **15:23:39**，服务 15:24:13 启动）。
  我 diff 出唯一差异：`_agent_tasks` 里 `new_results=int(count.get('n') or 0)` →
  `new_results=int(count['n']) if count and count['n'] is not None else 0`（修 `sqlite3.Row.get()` 崩溃）。
  也就是说：**修完之后在 deploy 之后再改了一次文件**，`after.sha256` 与 `backup_final` 都停在了上一步。
- 为什么算 blocker：这是上一轮 F1 的同型复发——"证据里的哈希 ≠ 线上字节"就再也无法证明线上跑的是哪一版；
  尤其这一次改动**刚好在服务启动前的 34 秒**，属于热修补，必须单独留痕。
- 需要修：把 `964b7ee5…` 写进新的 `after.sha256`（或新增 `after3.sha256`）并注明"deploy 后热修，
  15:23:39 落盘、15:24:13 启动生效"；`backup_final/companion_awareness.py` 重新同步为线上字节、
  另存一份 `pre-hotfix` 的 `15a0f024…`；本地 `life/companion_awareness.py` 同样是 `964b7ee5…`（一致 ✓），
  把这条差异写进 REPORT §1 的表格。

### high

**H1（上一轮 F5 未修，且报告描述与实测不符）**：`send` 仍谎报"已送达"
- 文件：`life/companion_agents.py:1182-1194`、`life/companion_agent_worker.py`
- 现象：只要有活进程且 `stdin` 可写，`applied='delivered'` 并输出"已送达正在运行的子 agent。"；
  但 `companion_agent_worker.py` 全文**不读 stdin**（我读过源码 + 实测：写进管道后 worker 没有任何读取路径）。
  REPORT §7.4 写的是"消息落台账标 `applied='stored'`"，与实测相反——**描述被写反了**。
- 修法：worker 增加 stdin 读线程（或在每个后端的循环里 poll），收到后写一条 `message_applied` 事件；
  `send` 只有在看到该事件后才是 `delivered`，否则 `stored` 且文案改成"已存进收件箱，当前进程不读"；
  同时改正 REPORT §7.4。

**H2（上一轮 F6 未复验）**：awareness 装配证据的 `observed_at` 与声称值不一致的记录没有被本轮覆盖
- 文件：`docs/../.acceptance/20261117-090800/messages-check.txt`（历史）、本轮报告
- 现象：t6 声称装配时 `observed_at=2026-11-17T09:07:02+08:00`，而 stage-a 证据文件里是 `09:09:23`；
  本轮**没有新的装配证据**（新的 `residence_probe.json` 只查了 `state_agent_tasks_present` 与
  `available_actions`，没有把 awareness 的 `observed_at`、账号 `state`、字节数打印出来）。
  线上能确认的是 `read_backend` 在 15:39:58 被写（说明 awareness 段真的在跑），但字节级拿不出来。
- 修法：本轮 STAMP 里加一份新的 `messages-check`（隔离副本 + 真 `Companion._messages()`），
  打印 `observed_at / account.state / awareness_bytes / request_has_private_keys`，并在报告里只引用这份。

**H3（有意保留的功能缺口，但影响"预算三模式可用"）**：`config.toml` 里仍无法选 `bounded_estimate`
- 文件：`life/spending.py:21-35`（`SpendingPolicy.parse()` 拒绝未知字段）
- 现象：REPORT §7.3 如实承认了，但把它归为"下一轮"。对用户可见的后果是：**只要把预算打开，
  永远只能得到 strict**，而 strict 下 dsh 与所有子 agent 派活都被拒——"三模式"在配置层仍是二态。
- 修法：`SpendingPolicy` 加 `mode: str = 'strict'` 字段并在 `parse()` 里校验
  `mode in {off,strict,bounded_estimate}`；`enabled=true` 无 `mode` 仍落 strict。这是十几行的改动，
  不该留到下一轮（否则"bounded_estimate 可过"只在测试里成立）。

**H4（上一轮 F7 部分修）**：可复跑资产仍有缺口
- 现象：`tools/stage-a/` 补回了 `test_companion_awareness.py` ✓；`tools/stage-b/` 现在存在 ✓；
  但 `tools/stage-a/companion_awareness.py` 仍是**旧版**（40964 B），与线上 44259 B 不一致；
  `.acceptance/20261117-144013-repai/` 里没有 `verify_awareness_agents.t9-original.py` 之外的
  "本轮可复跑脚本清单/入口"，`t13_*.sh` 分散在 `tools/stage-b/`，证据目录里没有指向它们的说明。
- 修法：`tools/stage-a/companion_awareness.py` 同步为线上字节或直接删除（它只是副本）；
  证据目录写一行 `how-to-reproduce`（哪几个 `.sh`、顺序、期望输出）。

### medium

**M1：生产库被服务写入后，验收链仍然指向它**
- 现象：`prod-stat-after.txt`（15:26:00，368525312 B）与现在的 `life.db`（368525312 B，mtime 15:38:21，
  WAL 9 MB）都在变——这是服务正常写库，不是验收写库（我确认过验收全在临时 HOME 副本）。
  但报告把"life.db mtime 被推进"写成一句"WAL 写入是常态"，没有给出"验收没有写生产库"的可核验证据
  （例如验收进程的 HOME 指向、`strace`/`lsof` 或临时目录清理记录）。
- 修法：验收脚本统一 `HOME=$(mktemp -d)` 并在证据里贴 `ls -d $HOME` 与该目录的 `TREE_CLEAN` 回执
  （t2 的 `run_isolated_suite.py` 已有这个能力，直接复用）。

**M2：t9 复跑 98/100，其中 1 条红 1 条 limited，不能写成"验收通过"**
- 现象：`repro_t9/run.log` 末尾 `SUMMARY {"total": 100, "passed": 98, "failed": 1, "limited": 1}`，
  失败项是 B3（现在返回 `parent_ambiguous`）。本轮报告在 §7.1 如实写了，但 t13 的任务输出里"t9 A6 转绿"
  容易被读成整体转绿。
- 修法：把判定条件补成"`nesting_limit` | `parent_finished` | `parent_ambiguous`（并断言 agent_id 路径
  的 0/1/2 与第 4 层拒绝）"，让 B3 用 **agent_id** 走一遍，红转绿要有真实断言而不是放宽。

**M3（上一轮 F1 的残留）：`.acceptance/LATEST` 仍指向旧目录**
- 现象：`/opt/qq-deepseek/.acceptance/LATEST` 内容仍是 `20261117-090800`，本轮目录没有被登记为最新；
  下游（P5 报告、D 阶段发布）按 LATEST 取证据会拿到上一轮的。
- 修法：`echo 20261117-144013-repai > .acceptance/LATEST`，并把"每次部署必须更新 LATEST"写进
  `docs/cloud-sync-policy.md` 的清单。

**M4（上一轮 F9 残留）：`_agent_worker_one` 的并发仍然"先读后领"**
- 文件：`life/companion.py:2760-2772`
- 现象：先 `_running_count() >= max` 判断，再 `queued(limit=1)` + `execute()`，两步之间没有锁；
  本轮没有改。单协程下无害，但"并发 3 真实生效"的判定点仍不在临界区里。
- 修法：`AgentManager.claim_next(max_concurrent)` 把计数与领取放进同一个 `_TABLE_LOCK` 事务。

**M5（上一轮 F10 残留）：`_agent_worker_one` 的 dsh 结果轮仍有覆盖风险**
- 文件：`life/companion.py:2781-2789`
- 现象：`payload=dict(..., action_result=dict(result), ...)` 与 `_cognize(..., dict(action_result=..., agent_id=..., project=...))`
  两处都塞 `action_result`；`_worker()` 当年就是为这个重复关键字抛过 `TypeError`。
  本轮没改，也没在真实服务里观测过这条路径。
- 修法：观测一次（哪怕用 `node=echo` + 隔离 HOME 起真服务），或先把两处合并成一处。

### low

**L1**：`companion_context.py` 仍是 CRLF（57 行，09:08:00 整文件重写留下的），本轮没修也没记录；
加 `.gitattributes`（`life/*.py text eol=lf`）一次性收口（上一轮 F8 的建议仍未落地）。

**L2**：`companion_awareness.py` 里留着 `_legacy_spending_mode()`（111-125 行）死代码，
`spending_mode()` 已转调 `budget_mode()`；删掉以免后人误用。

**L3**：`life/spending.py` 本地与云端 md5 都是 `b7fe7163…`，与 git HEAD 内容一致（仅行尾被 Git
警告会转 CRLF），不属于本轮改动；但它在 `before/after.sha256` 里被列为"未变"，值得在报告里
一句话说明"为什么把它列进去"（防误读成被改过）。

---

## 6. 上一轮 t8 十三条 finding 的现状

| 编号 | 内容 | 现状 |
|---|---|---|
| F1 | 生产版本不可证 | **部分修**：本轮 before/after 齐全、备份与回滚包齐；但 B2 让 after 再次对不上线上字节 |
| F2 | 阶段 B 无执行入口 | **已修**（上传链路成立，生产 0 行=尚未真实派活） |
| F3 | 预算三模式不可达 | **已修**（仅"配置里选模式"仍不可用，见 H3） |
| F4 | 删守卫换可用 | **已修**（守卫恢复且与子 agent 同裁决） |
| F5 | send 标 delivered 但 worker 不读 stdin | **未修**（且报告 §7.4 描述写反，见 H1） |
| F6 | stage-a 装配证据 observed_at 不符 | **未复验**（本轮无新装配证据，见 H2） |
| F7 | 可复跑脚本缺失 | **部分修**（stage-a 测试脚本补回、stage-b 存在；stage-a 副本仍旧，见 H4） |
| F8 | CRLF 整文件重写 | **已控**（executor/delivery 保持 CRLF，其余 LF；context.py 仍是 CRLF，见 L1） |
| F9 | 并发计数非原子 | **未修**（见 M4） |
| F10 | AGENT_INSTRUCTIONS/schema 与 result 覆盖 | **未修**（见 M5） |
| F11 | attention_set 无上界 | **未修**（未在本轮范围，保留） |
| F12 | dsh ready 路径无真实 cfg 用例 | **部分修**：本轮 `dsh-stub.txt` 建了 stub runtime，t9 的 B6 覆盖 strict/bounded 两类，但"有 dsh 且 off → ready"仍只靠单测 |
| F13 | 五档混为一谈 | **部分修**：REPORT 有了"上线状态"表和"未做"清单，但 §7.4 与 t9 98/100 的表述仍有误导（H1/M2） |

---

## 7. 放行建议

1. 先做 B1（属主）与 B2（after.sha256/backup_final 对齐 964b7ee5…）＋ M3（LATEST）——这三件不改代码，
   只是把证据链补成"可复现"；
2. 再做 H1（send 不再谎报）、H3（`SpendingPolicy.mode` 落地）——这两件动代码，改完按 §4 清单复跑
   并在新 STAMP 里出 before/after；
3. H2 的新装配证据与 M2 的 B3 改判一并出；
4. ④"真机验收"（真实 DSH 子 agent、真实 QQ 回执）在用户明确授权花费之后再谈，本轮一律记 **未做**。
