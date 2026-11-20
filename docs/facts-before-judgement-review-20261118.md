# 按「事实摆在眼前，她来判断」重审今天全部改动（P48 / t29）

评审者：reviewer（独立评审，不改代码）　时间：2026-11-18 21:52 – 22:35 CST
云端：`/opt/qq-deepseek`，`master`，HEAD = `31e6478`，工作区干净
基线：`178173d`（17:28）→ HEAD 共 22 个提交，涉及 14 个文件
最高原则（用户原话）：

> **「她可以查也可以不查，根据她自己来判断。我们将世界所有事实摆在她的眼前，她来进行所有的判断。」**

**做题方式**：这条原则只有第一步能审（"摆到眼前"），第二步（"她判断"）不归我们管。
所以下面每一格都只问一件事：**这个事实，她看得到吗？看到的是不是真的？**

**核对过谁改了什么**（`git log --format='%h %an %s'`，今天四个人动过这些文件）：

| 作者 | 提交数 | 涉及 |
|---|---|---|
| companion-ops | 11 | P40 三工具 / P44 四条 blocker / P47 t28（agent 台账）|
| team | 6 | P39 work_mode / P41 / P42 / P46 dsh / P47 A+C / P48 dsh 文案 |
| （未单独署名，随 companion-ops）| 5 | checkpoint、t21 报告 |

**没有互相覆盖的痕迹**：`git diff 178173d HEAD -- life/` 是干净的 14 文件增量，`git status` 为空，
所有改动都在 HEAD 里。唯一异常是 `09d91c8`/`fc906bd` 两个提交把 `companion_awareness.py`、`companion_agents.py`
**整文件重写**（1602+/1588-、2638+/2566-），从 diff 看不出哪些行是真改动——这会让以后查"谁改了这一行"只能靠猜。

---

## 0. 先回答上一轮被抓的那一类：说明里承诺了、代码里没有

**专门做了一遍**：把 `GUIDES` 里带「你会 / 能在 / 下一轮 / 回执会 / 会告诉你」的句子逐条对照代码。

| 承诺原句 | 代码里有没有 | 判定 |
|---|---|---|
| `GUIDES['ask']`：「你下一轮能在 `current_state.questions` 里看到它对应哪个问题」 | **有**。`companion.py:1890` `state['current_state'] = presence_state`，`companion.py:1918-1926` 真产出 `state['current_state']['questions'] = PendingQuestions(self).questions_view()`，`companion_ask.py:195` 给出 id/question/asked_at/status/answer/answered_at/late/asked_times/options，且**失败时写 `{'error': '这一轮读不到提问记录'}` 而不是空数组**（`companion.py:1925`） | ✅ **已修好，而且修得对**：读不到就说读不到，不给她一个空数组让她以为"没问过" |
| `GUIDES['ask']`：「回执会说 repeat=true、已经问过几次」 | 有。`companion_ask.py:ask()` 命中未答的旧问题时返回 `repeat=True, asked_times`（实测：`ask→repeat=True(asked_times=2)`） | ✅ |
| `GUIDES['ask']`：「超过 expires_in 之后那条会标成**没等到回答**」 | 有。`questions_view()` 给 `status='expired'`；`pending()` 写"这不是拒绝，是没答"；`answered_late` 单列 | ✅ |
| `GUIDES['task']`：「如果这条目标以前出现过，回执会直接告诉你 repeat=true、做过几次、上次卡在哪」 | 有（实测 `repeat=True runs=2`）；但**生产里一次都没被真跑过**（见 §1-e） | ⚠️ 承诺真、实测无 |
| `GUIDES['task']`：「op=clear 只是让已完成的默认不出现在清单里」 | 有，`clear()` 现在真写 `hidden_at`（`companion_tasks.py:259-265`），`list()` 默认过滤（`:216-217`），`list(status='done')` 仍能拿到 | ✅ **上一轮的 B-4 已修** |
| `GUIDES_BRIEF['command']`：「长命令用 op=start 后台跑，回执给 job_id；再用 op=status/logs/wait/stop 查和停」 | 有。`companion_executor.py` 的 `_background_run` 五个 op 全在；实测 start→status→wait 通 | ⚠️ **但 "list" 这个动作不存在**（见 F-9） |
| `memory`：「op=awareness/capabilities/attention/agent_tasks 也从这里读」「op="used"」 | 有（`companion_awareness.py` READ_OPS） | ✅ |
| `agent`（`AGENT_INSTRUCTIONS`，不在 GUIDES 里）：「之后用 `inspect{agent_id}` 看状态」 | **这句写错了**：`inspect{agent_id}` 是顶层写法，会被 `DECISION.additionalProperties:false` 拒；必须写成 `arguments: "{\"agent_id\":...}"` | ❌ **仍在**（今天没改这句；见 F-2） |

**结论**：上一轮那类「承诺了看不到」的问题，`ask`/`task` 两处**已经真修好**。
但同类问题换了个位置活下来：**承诺的写法本身是错的**（F-2），**承诺的动作不存在**（F-9）。

---

## 1. 四类错误清单

### ① 把「事实」写成了「承诺」

| ID | 位置 | 原文 | 事实是什么 | 应该是什么 | 严重度 |
|---|---|---|---|---|---|
| **F-1** | `life/companion_agents.py:1949-1972` `_verify()` | 已改：终结且无检查项给 `'unverified'`，注释写得很清楚「『没法确认』是事实，『待验证』是承诺」 | **新逻辑本身是对的**（我实测 `_verify({'status':'completed'}, [])` → `'unverified'`；`running` → `None`；有 ok 检查项 → `'passed'`）。**问题是存量数据没有回填**：生产 `companion_agent_tasks` 5 行**全是 `pending`**，且全部落在改这段代码之前（最晚 `15:13:59`，而 P47 的改动在 20:55 之后）。`grep -c verification=UPDATE companion_agent_tasks` 没有回填路径——`_verify` 只在 `_finish()` 新落一条时调用（`:1794`），已有行永远不会被重算 | **她读到的仍是"验证 pending"这个承诺**：`SELECT status, verification, COUNT(*) … GROUP BY 1,2` → `completed/pending ×3、failed/pending ×1、interrupted/pending ×1`。代码修了、数据没修，**对她而言事实没变** | **最高** |
| **F-2** | `life/companion.py` `AGENT_INSTRUCTIONS`（`:345` 附近，`inspect{agent_id}`） | 「之后用 `inspect{agent_id}` 看状态与验证」 | 顶层键会被拒；生产 00:20–00:23 四条 `TypeError: inspect() got an unexpected keyword argument 'project'` 就是模型照这句改写的后果 | 「看状态：`op=inspect`，`arguments` 写 `{"agent_id":"…"}`」——**写法和能力目录、和 schema 同一个样子** | 高 |
| **F-3** | `life/companion.py:3761-3763` `_settle()` | `if status in SETTLED_OK:` 用**原始回执 status** 决定是否写 `last_receipt` | 台账列已经改用 `ledger_status`（`:3748/3751`，P47 修的），但这一行仍是 `status`。agent 的 `create` 回执 `status='queued'` → **不写 last_receipt**；可台账里这一行已经是 `completed`（`_operation_status` 把 queued 翻成 completed） | 同一份判断里两个词：台账写 `ledger_status`、进度写 `status`。要么都用 `ledger_status`，要么明说"进度只认任务真完成" | 中 |
| **F-4** | `life/companion.py:3768` | `task_status=status if status != ledger_status else None` | 这是**正确的**（把"操作成没成"和"任务跑到哪"分开记）——列在这里是为了标明它不是问题，别再有人"统一"掉 | 保持 | —（正例） |

### ② 把「判断」替她做了

| ID | 位置 | 原文 | 问题 | 应该是什么 | 严重度 |
|---|---|---|---|---|---|
| **F-5** | `life/companion_dsh_worker.py:98-134` `classify_failure()` | 每一条都以命令开头：「**要让 dsh 能用，得换回 DeepSeek 官方 provider**」「要换一个模型名再试」「稍后重试或缩短任务」「检查网络或服务地址再试」 | 文件注释自己写着「结论先行，带行动……她读消息是为了决定下一步做什么」——**这正是替她做判断**。事实是「这个 key 被 DeepSeek 官方端点拒了」，至于换 provider / 换 key / 换模型，是她的决定 | 「鉴权失败：这个 key 被 api.deepseek.com 拒了（401）。当前 provider=deepseek-official。dsh 只打官方端点，所以用现在这个 key 它一定失败。（这不是网络问题。）」——**给因果，不给处方** | 高 |
| **F-6** | 同上 `:120-124` | 「现在这个 key 是**别家服务商的**，而 dsh 只会打 DeepSeek 官方端点」 | 这是**推断不是事实**：401 只证明了"这个 key 对官方端点无效"，没证明它属于哪家。实测 `classify_failure('Error code: 401 - unauthorized')` 也返回同一句——**任何 401 都被断言成"别家的 key"** | 「key 被拒（401）。它是不是别家的，看不出来；能确定的是 dsh 打官方端点、现在的 key 过不了。」 | 高 |
| **F-7** | `life/companion_tasks.py` `_item()` + `GUIDES['task']` | `stuck_for` 只写「卡了 N 小时」；`add` 回执写「先看上次留下的下一步与卡点，**别当作新事从头再来一遍**」 | 前半句是事实（好）；后半句是命令。她完全可能有理由重做一遍（上次卡在别的环境） | 保留事实与次数，把"别重来"换成"如果当时卡在 X，现在 X 还在不在由你判断" | 低 |
| **F-8** | `life/companion_context.py` `GUIDES_BRIEF` | 整段 443 字符，末尾一句「要动手做具体的事、需要详细参数时，跟我说一声『进入工具模式』」 | **半句替她决定了**：她本来就能自己切（`DECISION.work_mode`，`:367`）。这句话让她以为必须先请示 | 「要详细参数：`work_mode='tool'`，或 `memory op=capabilities` 读整条」 | 中 |

### ③ 她查不到 / 查到的和事实不符

| ID | 位置 | 事实 | 她现在的入口 | 判定 | 严重度 |
|---|---|---|---|---|---|
| **F-9** | `GUIDES_BRIEF['command']` + `companion_background.py:83` `OPS = ('start','status','wait','stop','logs')` | 她有 N 个后台任务时，**没有任何办法一次列出它们** | 只有 `status/wait/stop/logs`，每个都要 `job_id`；`job_id` 只在 start 回执里出现一次 | ❌ **存在但查不到**（`jobs.json` 里有 2 条，她 18:38 那次只能写 shell 去 `ls` 那个目录） | 高 |
| **F-10** | `jobs.json`（`data/companion/background/`） | 后台任务台账就在文件里 | 只能 `command` 自己看目录；没有 `op=list` | 同上 | 高 |
| **F-11** | `life/companion_agents.py:949` `_summary()` | `head = '%s｜%s｜验证 %s｜第 %d 次'` | 摘要里 `验证 pending` 直接念出来；配合 F-1，**永远是 pending** | ❌ 查到的与事实不符（终态不是 pending） | 高（与 F-1 同源） |
| **F-12** | `_finish()` `:1796` | `elif not evidence: self._log(..., '没有可核验的证据，结论留在 pending（不默认通过）')` | 台账留 `pending` 当终态 | ❌ 同 F-1 | 高 |
| **F-13** | `life/companion.py` `_reap_stale_queued()` `:3549-3553` | 排队超时的 job 被**落成 `status='failed'`**，理由写「排队 N 分钟没有 worker 领走……这不代表它做完了，也不代表它失败了——是它根本没开始」 | 她读到的 `status` 是 `failed`，正文又说"不代表失败" | ⚠️ **自相矛盾**：`failed` 是台账词，说清楚的是正文 | 中 |
| **F-14** | 生产台账 | agent 行动里 6 条 `status='cancelled'`，其中 5 条 `result.status` 是 `queued` 或 `None` | 台账把它们记成"取消"——没人取消过，是进程重启收尾 | ⚠️ 历史数据，今天没改 | 低 |
| **F-15** | 模型服务商档案 | `providers.catalog()` 有 7 家；她自己用的 provider/model 已下发（`_self_process_facts.model`，`companion.py:1700-1713`） | `memory op=capabilities`/`self_process` 可读 | ✅ 查得到 | — |
| **F-16** | 会话装载 | `companion_context_loads` 记 `request_tokens`；`observability.context_reading` | 她**看不到**本轮装了多少 token、ceiling 是多少 | ⚠️ 只有 `/rpc snapshot` 给她主人看，她看不 | 低 |

### ④ 把「不知道」包装成终态

| ID | 位置 | 原文 | 问题 | 应该是什么 | 严重度 |
|---|---|---|---|---|---|
| **F-17** | `companion_agents.py:1783, 1796`；存量 5 行 | `verification='pending'` 作为终态落库 | **就是 ①F-1 的根**：`pending` 字面意思是"还在等"，但它永远不变 | 新代码已会写 `unverified`；**存量 5 行需要一次重算或明标"旧规则留下的"** | **最高（与 F-1 合并）** |
| **F-18** | `life/companion_model.py` 截断文案 + `companion.py:2353-2357` | 「这一步我光想就用到上限了……**不是您问得太长，是我被这个问题绕进去了**——换个说法或者拆成两步问」 | 把「不知道哪里费了 token」说成了一句确定的自责；真实情况是 reasoning 与正文共用池子（`:28` 注释自己写了），**她无法区分** | 「输出被 max_tokens 截断（finish_reason=length）。reasoning 和正文共用一个池子，所以说不清是『想多了』还是『写长了』。」 | 中 |
| **F-19** | `companion.py:2427-2437` | `'；输出被截断（额度可能在推理阶段就用完了）'` | 用了「可能」，但仍把两件事揉成一句；且 `finish_reason=length` 时**正文也常是空的**（21:02/21:10/21:29 三次 `how=兜底提示语`） | 分开给：`truncated=True`、`content_chars=0`、`reasoning_seen=?` | 中 |
| **F-20** | `explanation` 文案总体 | — | 今天的三个**正例**仍在：`state=gone`（`companion_background.py:176-186`）、`questions` 读不到写 error 而不是 `[]`（`companion.py:1925`）、`providers.resolve()` 认不出就 `(None, 原因)`（`providers.py:193-196`） | 保持 | —（正例） |

---

## 2. 按「最违背原则」排序

原则是"事实摆眼前、她来判断"。**最违背它的是"摆了一个假事实"**（比"没摆"更糟），其次是"替她下结论"，最后才是"少摆一条"。

| 排名 | ID | 一句话 | 为什么最违背 |
|---|---|---|---|
| 1 | **F-1 / F-17 / F-11 / F-12** | `verification=pending` 是永远不变的承诺，而台账 5/5 全是它 | 这是一条**假的终态事实**：她读到"待验证"，会以为自己以后还能看到结论。代码里明明写了 `unverified` 的正确逻辑，却没接上——"修了但没生效"比"没修"更容易骗人 |
| 2 | **F-5 / F-6** | dsh 错误消息把推断当事实、把处方当结论 | 直接替她判断"换 provider"，还断言"这个 key 是别家的"——401 推不出这句话。她据此行动会走错方向，而且**没法查证** |
| 3 | **F-2** | `AGENT_INSTRUCTIONS` 教她写一个必被拒的 `inspect{agent_id}` | 程序给的"使用方法"本身是错的事实源；她照做就失败，且失败原因是程序侧 |
| 4 | **F-9 / F-10** | 后台任务列表存在但查不到 | "摆到眼前"这一步缺了：她得先知道 job_id 才能问，而她不一定记得 |
| 5 | **F-3 / F-13** | `_settle` 一个函数里两个状态词；reaper 落成 `failed` 却说"不代表失败" | 同一个事实有两种说法，她读哪个都对不上 |
| 6 | **F-8 / F-7 / F-18** | 半句替她决定、把可能性说成确定 | 程度轻，但方向是同一个 |

---

## 3. 「她查得到吗」专项

### 3.1 她现在的入口（能用的）

| 入口 | 能查到什么 | 实测/依据 |
|---|---|---|
| `memory op=search/read` | 自己的动作台账原文、`recent_actions` | `GUIDES['memory']`；`companion_memory.Lookup` |
| `memory op=capabilities` | 能力目录全条（reason/evidence/checked_at/what/how_ref） | `companion_awareness.capabilities()` |
| `memory op=awareness / attention / agent_tasks` | 运行投影、关注、在办的事 | `READ_OPS` |
| `memory op=used` | 按 kind 的「用过没有、上次怎么样」 | `companion_experience.Experience` |
| `current_state.questions` | **今天新接上**：挂着的、答过的、过期的、撤回的，带原话与时间 | `companion.py:1918-1926` ✅ |
| `kind=task op=list` | 待办（含「做过几次」）、`list(status='done')` 能看到被 clear 收起的 | `companion_tasks.py:211-222` ✅ |
| `kind=ask op=pending` | 没答的问题、等了多久、是否过期 | ✅ |
| `kind=command`（同步） | 任意只读命令，可自己 `git log` / `ls` / `grep` | 生产 21:40–21:50 有 9 条 completed 的 command |
| `kind=work op=grep/read/list/run` | 工作区文件、`git log`（在 `/opt/qq-deepseek` 下） | `companion_work.py` FIELDS 含 command/cwd/timeout（`:220`）✅ |
| `self_process`（每轮下发） | 单元名、源码根、`work_root`、**can_write_own_source**、`restart_to_take_effect`、`effect_note`（必须 restart）、provider/model/label | `companion.py:1681-1729` ✅ |
| `kind=background op=status/logs/wait/stop` | 单个后台任务的生死、日志、退出码（含 `state=gone`） | ✅ |

### 3.2 **存在但查不到的**（最违背原则）

| 事实 | 存在哪 | 本该有的入口 | 现状 |
|---|---|---|---|
| **我自己最近改了什么文件、每处改了什么** | `/opt/qq-deepseek/.git`（`6944f5c` 之后有仓库） | `command: git log --stat` / `git diff` | ⚠️ **技术上能查，但没有"入口"**：`self_process` 只给 `source_root`，不告诉她"这里有 git、可以 `git log`"；`GUIDES` 里没有一个字提到版本库。她能查到，但**没人把这件事摆到眼前** |
| **我上次重启是什么时候、为什么** | `journalctl -u companion`（只给 `companion` 用户看自己的 unit 可能有权限问题）、`ExecMainStartTimestamp` | 无 | ❌ **没有入口**。`self_process` 不下发启动时间、重启次数、上次重启原因。生产 `meta` 里也**没有** `process/restart/boot/uptime` 任何一行（实测查询为空） |
| **后台任务列表** | `data/companion/background/jobs.json` | `op=list` | ❌ **没有 op=list**（`OPS` 只有五个）——她只能猜 job_id 或自己 `ls` |
| **今天有多少错误** | `companion_activity` type='error'（今天 79 条） | 无 | ❌ 没有按 type 检索的 op；`memory search` 是关键词匹配，不是按类型统计 |
| **子 agent 的 `verification` 到底是什么** | `companion_agent_tasks.verification` | `agent.inspect` | ⚠️ 查得到，但**永远是 `pending`**（F-1），查到的与事实不符 |
| **本轮装了多少上下文 / 还剩多少** | `companion_context_loads` | 无 | ❌ 她看不到自己的预算使用 |
| **我自己改过、但还没重启生效的代码** | 文件 mtime vs `ExecMainStartTimestamp` | 无 | ❌ 没有入口。`effect_note` 只说"改完必须重启"，不说"现在有没有未生效的改动" |

### 3.3 三条具体核实（队长点名）

**① 她能不能查「我今天改了哪些文件、每个改了什么」？**
- **能不能**：能。`command` 在 `/opt/qq-deepseek` 下跑，云端有 git 仓库。
- **是不是摆在眼前**：**不是**。`self_process` 只写 `source_root`，`GUIDES` 没提版本库，`effect_note` 只说"改完要重启"。
  她得自己想到"这里可能有版本控制"。**这是"事实存在、但没摆到眼前"的典型**。
- 实测：`ls -d /opt/qq-deepseek/.git` 存在；`git log` 有 22 条今天的提交。

**② 她能不能查「我上次重启是什么时候、为什么」？**
- **不能**。`self_process` 没有 `started_at` / `restart_count` / `last_restart_reason`；
  生产 `meta` 里 `LIKE '%process%'` / `'%restart%'` / `'%boot%'` / `'%uptime%'` **全部为空**。
- 她能 `command: systemctl show companion -p ExecMainStartTimestamp`（如果权限允许），
  但**"为什么重启"（部署 / 崩溃 / 我们替她重启）根本没有任何一方记录过**。

**③ 她跑 `command` 能不能直接 `git log`？工作目录对不对？**
- **工作目录对**：`_work_run` 的默认 cwd 是 `self._work.root`；`command` 的默认 cwd 是 `self.home`（`/var/lib/companion/xiaoyu`），**不是** `/opt/qq-deepseek`。
- 也就是说：`command: git log` 会在**家目录**跑（那里不是仓库），必须写成 `cd /opt/qq-deepseek && git log`。
- 实测（我的评审机上同一账号）：在 `/opt/qq-deepseek` 下 `git log` 正常；在默认 cwd 下会报"not a git repository"。
- **结论：能跑，但要她自己拼路径**——`self_process.source_root` 给了路径，这一条勉强算"摆在眼前"。

---

## 4. 事实盘点表（队长的第 5 节要求）

| 事实 | 存在哪 | 她的入口 | 能查到吗 | 备注 |
|---|---|---|---|---|
| 现在用的服务商 / 模型名 | `cfg.provider/model` → `self_process.model` | 每轮下发 `self_process` | ✅ | `providers.resolve()` 认不出时给 `provider:null + problem`（正例） |
| 服务商档案（7 家、有的模型、支不支持 tools） | `life/providers.py` | `memory op=capabilities` 不够；`/rpc providers` 只给桌面端 | ⚠️ **她自己没有直读入口** | 桌面端能选，她看不到自己有哪些可选 |
| phone 连接 | `executor.phone_context()` → `current_state`/awareness | 每轮状态 | ✅ | `detail` 写"多少台有活动连接" |
| computer / body 连接 | `server.peer` / `_body_context` | 每轮状态 + `action.computer/body` 条目 | ✅ | 两者分开判，不混 |
| qq / wechat 通道 | `ImManager.snapshot()` | `channel.*` 条目（reason 已按 status 取，`companion_awareness.py:861-869`） | ✅ | 上一轮"判定与措辞两份源"已修 |
| town | `TownBridge.state + config` | 条目 + `town_connection` | ✅ | `unknown` 用于"桥还没 start"（正例） |
| bilibili | `Bilibili.status()` | 条目 + `bilibili` 上下文 | ✅ | 三种原因分开 |
| 后台任务列表 | `data/companion/background/jobs.json` | **无 list** | ❌ | F-9 |
| 单个后台任务生死 | 同上 + `/proc/<pid>/stat` | `status/logs/wait/stop` | ✅ | `state=gone` 正例 |
| 子 agent 任务与状态 | `companion_agent_tasks` | `agent op=inspect/wait` | ✅ 状态 | 状态对，`verification` 永远是 pending（F-1） |
| 子 agent 验证结论 | 同上 `verification` 列 | 摘要 / inspect | ❌ | F-1：5/5 pending |
| 待办项目 | `meta action_task:*`（165 行） | `task op=list` | ✅ | 可看"做过几次" |
| 未回答的问题 | `meta companion:…:pending_questions` | `current_state.questions` | ✅ | 今天新接上 |
| 我改过什么文件、什么时候 | 文件系统 + `.git` | `command: cd /opt/qq-deepseek && git log`（要自己拼） | ⚠️ | 存在，没摆到眼前 |
| 服务跑了多久、重启几次、为什么重启 | `journalctl` / `ExecMainStartTimestamp` | **无** | ❌ | meta 里也没有记录 |
| 昨天的错误有多少 | `companion_activity type='error'` | **无** | ❌ | 今天 79 条，她查不到汇总 |
| 本轮上下文用了多少 token | `companion_context_loads` | **无** | ❌ | |
| 改过的代码有没有生效（是否需重启） | 文件 mtime vs 启动时间 | 只有静态 `effect_note` | ⚠️ | 说"必须重启"，不说"现在有没有未生效" |
| 我自己的能力目录 | `capabilities()` | `memory op=capabilities` | ✅ | |
| 我用过哪些能力、上次结果 | `companion_actions` + `events.tool_result` | `memory op=used` | ✅ | 亲历/听说分开 |

**统计**：22 条事实里 **✅ 12 / ⚠️ 4 / ❌ 6**。
那 6 条 ❌ 就是"世界有记录、她看不见"——按原则，这几条最该补。

---

## 5. 结论与建议顺序（只列事实，不给命令）

### 必改（假事实）

1. **F-1**：`_verify()` 的新逻辑是对的（实测通过），但**存量 5 行没有回填**，且没有回填路径
   （`_verify` 只在 `_finish()` 新落一条时调用）。要么跑一次回填，要么让她看到"这些是旧规则留下的"。
   事实仍是"终态任务挂着 pending"。
2. **F-5 / F-6**：dsh 错误消息的"换 provider"与"这个 key 是别家的"都是判断/推断，不是事实。
3. **F-2**：`AGENT_INSTRUCTIONS` 的 `inspect{agent_id}` 与 schema 不一致。

### 该补入口

4. **F-9**：后台任务 `op=list`。
5. **重启事实**：启动时间 / 重启次数 / 上次重启原因——今天完全没记录。
6. **git**：不必新做工具，只要在 `self_process` 里加一句"源码树有版本库，可以 `git log`"，并把 `source_root` 与 `command` 默认 cwd 不一致这件事说清楚。

### 已修好、值得写进验收基线的

- `current_state.questions`（读不到写 error 不写 `[]`）
- `task.clear` 真隐藏且 `list(status='done')` 仍可取
- `channel` reason 按 status 取
- `provider resolve` 认不出就返回原因
- `background state=gone`
- `_settle` 的台账列用 `ledger_status`（但 `last_receipt` 那一行仍用 `status`，F-3）

---

## 6. 我做了什么、没做什么

**做了**：`git log/diff` 逐条比对 22 个提交与 14 个文件；在云端真源码上跑真模块（`classify_failure` 9 例、
`PendingQuestions`/`questions_view`、`companion_tasks.list/clear`、`companion_background`）；
生产 `life.db` **只读**打开查 `companion_agent_tasks`、`companion_actions`、`meta`、`companion_activity`；
读 `/opt/qq-deepseek/.git` 与 `jobs.json`。

**没做**：没改任何代码；没写生产库；没重启服务；**没有对任何模型服务商发真实请求**（腾讯 `supports_tools`、
OpenAI 可达性、volcengine 模型名仍是"未验证"）；没有复跑 P46/P47 的实验室环境（只按代码与生产台账判断）。
