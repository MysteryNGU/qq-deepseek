# P43 独立能力评审：今天全部改动的正确性 / 一致性 / 可发现性 / 诚实性

评审者：reviewer（独立评审，不参与实现）　时间：2026-11-18 18:49 – 19:20 CST
对象：云端 `/opt/qq-deepseek`（`master`，HEAD = `87d9bdb`，工作区另有未提交改动，见 §0.3）
基线：`178173d`（`baseline: 2026-11-18 全部修复后的生产版本`，17:28）
方法：`git diff 178173d HEAD` 逐项比对 + 在云端真源码上跑真模块（临时库 / 临时 HOME，**不碰生产数据、不改代码**）+ 生产只读台账复核
**不采信任何人的转述**：下面每个结论都附了我自己跑出来的输出或文件行号。

---

## 0. 先说三件必须先对齐的事实

### 0.1 报告里的 md5 —— 6 条里 5 条对得上，`companion.py` 那条对不上（且**不是**被改坏了）

`docs/p40-three-tools-20261118.md` 头部的 md5 表，我逐个重算：

| 文件 | 报告写的 | 我算出来的（云端当前） | 判定 |
|---|---|---|---|
| `life/companion_background.py` | `ad767d64` | `ad767d645ddb2519546c9b5a37e7fe62` | ✅ 一致 |
| `life/companion_tasks.py` | `e817a3e9` | `e817a3e91ef6a9a4b1089c91a43a2380` | ✅ 一致 |
| `life/companion_ask.py` | `2f8d998b` | `2f8d998ba71d922100f4ef0b07acd5e0` | ✅ 一致 |
| `life/companion_executor.py` | `6235165b` | `6235165b222167e399b162a54c25ff7a` | ✅ 一致 |
| `life/companion_context.py` | `96b4810e` | `96b4810e9197750f4f6a1858e1131193` | ✅ 一致 |
| `life/companion.py` | `5e76ba37` | **`c9f0d399…`**（工作区）/ `12047b02…`（HEAD） | ❌ 对不上 |

- `5e76ba37` = `git show 2683968:life/companion.py | md5sum` → 那是 **P40-3 提交那一刻**的版本。
- 也就是说：**文档写的是"我提交时的 md5"，不是"现在生产上跑的 md5"**。P41/P42 之后 `companion.py` 又改了两轮，文档没跟着更新。
- **这是文档新鲜度问题，不是代码问题**：指向"我现在跑的是不是这一版"的那张表，本身已经过期。建议改成"随版本一起自动生成，或改成 `git rev-parse HEAD` + 变更文件清单"。
- 顺带一个反例：`companion.py` 工作区版本（`c9f0d399…`）与 `commit` 版本（`12047b02…`、`git show HEAD`）都**不是** `5e76ba37`——那句话只在 P40 那次提交后 20 分钟内为真。

### 0.2 评审范围里第 4 组（"其他修复"）**在云端一次都没发生**

任务书点名的四条：`work` 的 `run` op、`_work_run` 不退回 goal、`execute` 不无条件改写 `return_code`、`memory`/`skill` 跨项目作证据、结果轮认知阻塞 worker。

- `git diff 178173d HEAD -- life/` 只涉及 10 个文件：`companion.py` / `companion_ask.py` / `companion_background.py` / `companion_context.py` / `companion_executor.py` / `companion_model.py` / `companion_remote.py` / `companion_tasks.py` / `providers.py` / `role_config.py`。
- **`life/companion_agents.py` 自 `178173d` 起零改动**（`git diff 178173d HEAD --stat` 里没有它）：`return_code` 仍是 `result.get('return_code', tree.process.returncode)`（`companion_agents.py:2373`），无条件改写那处**没有动过**。
- `git log --all` 里没有任何 P43 / 跨项目 / worker / return_code 相关提交。
- 但生产台账里确实有"自然语言被当命令"的实例（18:37:58 `command` 失败：`/usr/bin/sh: 1: 列出后台任务目录、取最新日志内容，找出: not found`），说明这个坑**还在**。

**结论：第 4 组只能按"未见改动、未见证据"记，不能算完成。** 见 §4 的 blocker S-1。

### 0.3 评审期间生产在工作区有一份**未提交**的改动，且服务正在跑它以外的版本

- `18:39:57`、`18:49:39` 两次重启；`18:46`–`18:49` 期间 `companion.py`、`companion_model.py`、`companion_remote.py`、`providers.py` 被改（工作区有未提交差异）。
- 我开工时（18:49:39 起）跑的进程用的是**磁盘上那一份**，但 `git diff HEAD` 显示 `companion.py`/`companion_model.py`/`providers.py`/`companion_remote.py` 与 HEAD 有差异；后来 HEAD 又前进到 `87d9bdb` 并变干净。
- 这意味着：**"部署版本 == 仓库版本"这个前提在我评审期间不成立**。凡是依赖具体版本的判断，下面都写了取数时刻。

---

## 1. 一张表：每项改动 × 正确性 / 一致性 / 可发现性 / 诚实性

约定：每格先给判定（✅ 通过 / ⚠️ 有条件 / ❌ 不通过 / ⬜ 未验证），再给证据。

| # | 改动 | 正确性（有没有回归） | 一致性（同一概念说法统一吗） | 可发现性（她能不能在上下文里看到） | 诚实性（有没有把"不知道"说成"知道"） |
|---|---|---|---|---|---|
| 1 | `life/companion_world.py` 统一事实层（6 条 fact） | ✅ 新增模块，`178173d→HEAD` 里没有它的删除/改写；`life` 整包真 import 通过（见 §2 命令 `IMPORT`）。6 条 fact 全部经 `_world_fact()` 降级：事实层抛错 → `unknown()`，不拖垮能力判断（`companion_awareness.py:477-489`） | ✅ 这是**今天唯一一处把三套词表收成一套**的地方：`STATES = ready/needs_setup/offline/unsupported/unknown`，与 `capabilities` 同源（`companion_world.py:39`）。`channel_fact()` 把 `ACCOUNT_STATES` 6 词映射到 5 词，**映射不全就走 unknown 而不是猜**（`companion_world.py:212-218`） | ⚠️ 事实层本身**不进主上下文**：`brief()` 只留 `state + detail`，而且 `life/companion.py` 里**没有任何地方引用 `companion_world`**（`grep -rn companion_world life/companion.py` 无结果）。她能看到的只是 awareness 二次加工后的一句 `detail` | ✅ **正例很硬**：`town_fact()` 区分"配置没启用"与"桥还没 start"（`companion_world.py:246-268`），后者给 `unknown` 并写明"连接还没开始，不能确认"；`closed` 状态单独判 unknown，理由写"不是配置问题"（`:236-240`）；`channel_fact` 的 `disabled` 映射到 `needs_setup` 并附"这是消息通道，不是某个动作" |
| 2 | `companion_awareness.py` 五处写死判据改读事实源 | ⚠️ 有新回归风险（见 §3 B-3：`channel.qq` 状态与 reason 用两份源）；`body`/`computer`/`dsh`/`town`/`bilibili` 五处改得干净（`:536-586`） | ⚠️ **通道这一处不自洽**：`status_` 用 `world_fact['state']`，`reason` 却用 `channel_reason.get(state_)`（原始 `ACCOUNT_STATES`）、`usable` 也用 `state_ == 'connected'`（`companion_awareness.py:849-870`）。同一个条目里**判定用一套词、解释用另一套词** | ✅ `what` 仍在每条上（`_action_entry`），`_offline_reason` 的 phone 分支改成**不再写死 90 秒**（`:517-528`，注释明说"常量一改，写死的数字就会撒谎"） | ✅ `_unknown_reason()` 统一口径"我读不到…，不能说明它坏了"（`:463-470`）；`phone` 探测抛错 → unknown，执行器没给 `phone_context` → unknown，两者**不再混成"本机没有手机控制通道"** |
| 3 | `companion_context.py`：`GUIDES`（详细）/ `GUIDES_BRIEF`（简易） | ✅ `tool_instructions(kinds, brief=False)` 默认走 `GUIDES`，旧调用点行为不变（`:61-63`） | ⚠️ **同一个概念两份文案，已经开始漂移**：`GUIDES` 14 种，`GUIDES_BRIEF` 14 种，集合一致（实测 `enum-GUIDES = ['agent']`、`BRIEF-enum = []`），但**内容粒度差一个量级**（详细 6809 字符 / 简易 459 字符），且 `ask` 的 detailed 版承诺了 `current_state.questions`（见 #5） | ⚠️ **可发现性比任务书设想的要好，但有一个真缺口**：`GUIDES_BRIEF` 里 `task`（48 字）和 `ask`（50 字）**都在**，后台 op 也写进了 `command` 行（"长命令用 op=start 后台跑，回执给 job_id；再用 op=status/logs/wait/stop 查和停"）。**没有漏项**。实测简易版整段 **443 字符**（不是 300），详细版 **5252 字符**（不是 5384——随 `agent` kind 产生差异，见下） | ⚠️ 简易版末尾那句"跟我说一声「进入工具模式」"把切换权一半交回给了用户（她自己也能切，见 #4）；这不算撒谎，但措辞会让她以为必须请示 |
| 4 | `companion.py`：`work_mode` 生活/工具，她自己切 | ✅ 纯增量：`DECISION.work_mode` enum `['life','tool']`（`companion.py:367`），`_apply_work_mode()` 不传就保持原值（`:628-634`）；`_commit()` 里先应用（`:2689`） | ⚠️ **两个 `work_mode`，同一个名字两种语义**：顶层 `work_mode`（她选的持久模式，`meta` 存）与函数里的 `tool_mode = purpose == 'work' or forced_mode == 'tool'`（`:1730`，**本轮用途**）。`purpose='work'` 的轮次**会**用详细说明、但 `meta.work_mode` 仍是 `life`——她若问"我现在是不是工具模式"，按持久值答会和实际生效的不一致 | ✅ 实际生效：`tool_instructions_with_offline(kinds, unavailable, brief=not tool_mode)`（`:1994`）。生产证据：`companion_activity` 里 18:37:58「她自己切到了工具模式」、18:38:38「她自己切回了生活模式」，`meta.companion:xiaoyu:work_mode = "life"` | ✅ 审计写明 from/to（`:643`），且 `try/except TypeError` 只在 `_audit` 不接受 kwargs 时退化，不吞别的问题 |
| 5 | `kind=ask` 结构化提问 | ✅ `PendingQuestions` 我用隔离 core 跑过全生命周期：ask → repeat=True(asked_times=2) → pending(waiting) → answer(matched=1) → 再 answer(matched=0) → 过期后 answer 标 `answered_late=True`。生产也有真实一轮（`ask_4ac7c3f02a9c`，18:13 登记，18:14 认领，`answered_late=false`） | ⚠️ **状态词第四套**：`waiting` / `expired` / `cancelled`（用 `answered=True` 表示）/ 无独立 `done`。`pending()` 里 `state='expired' if expired else 'waiting'`（`companion_ask.py:174`），但 `cancel` 之后该条**隐含**在 `answered` 里，`pending()` 不再列出——她能看到的只有"没挂着"，看不到"问过又被谁撤了" | ❌ **最严重的一条**：`GUIDES['ask']` 明写「你下一轮能在 `current_state.questions` 里看到它对应哪个问题」（`companion_context.py:31`），但**全仓库没有任何地方产出 `current_state.questions`**（`grep -rn 'current_state.questions' life/` 只命中这一行文档）；`PendingQuestions.context()`（`companion_ask.py:196-210`）**没有任何调用方**；`self._answered_question`（`companion.py:2097`）**只赋值、从不读**。**这是"把不知道说成了知道"的教科书式反例：程序在提示词里承诺了一条她拿不到的信息** | ✅ 超时不等于拒绝：`pending()` 明写"已经超过等的时间，没等到回答——这不是拒绝，是没答"（`companion_ask.py:185-187`）；`cancel` 不写成答过（"撤回不等于主人答过"）；`GUIDES['ask']` 也写了"别自己替他补一个答案" |
| 6 | `kind=task` 任务清单（接现有项目记录） | ✅ 我用隔离 sqlite 跑过：`add` 两次同义目标（"修好APK三处体验" vs "…体验！"）→ 第二次 `repeat=True runs=2`，`meta` **仍只有 1 行**；`list` 输出"这件事做过 2 次"；未知 ref 抛 `KeyError`（由 `_task_run` 转成可读回执）。生产 `meta` 里 163 条 `action_task` 行、**没有新建表**（`SELECT name FROM sqlite_master WHERE name IN ('companion_tasks','tasks','tasks_list','pending_questions')` → `[]`） | ⚠️ **`clear` 名不副实**：它**什么也不写**，只数了已完成条数然后说"清单默认不列它们"（`companion_tasks.py:246-253`）——而 `list()` **根本没有过滤已完成的**（`order` 里 `done` 排最后但仍在列表里）。她的 `GUIDES['task']` 写"op=clear 只是让已完成的默认不出现在清单里"，**这句话当前不成立** | ✅ 简易版与详细版都在（见 #3）；详细版把"动手前先 op=list"写成了硬建议 | ✅ `_count()` 遇到列表/字符串一律当"不知道"而不是 `int()` 崩（`companion_tasks.py:46-58`，正是生产 18:05-18:06 那三次 `TypeError: int() argument … not 'list'` 的修复）；`stuck_for` 写"多久没动过"并注明"不是失败" |

---

## 2. 新增工具的实测证据（我跑的，不是转述）

命令在云端 `/opt/qq-deepseek` 下执行，全部用**临时目录/临时库**，生产 `life.db` 只读打开。

```
IMPORT  : 整包 import life → "FULL PKG OK"；十个改动模块逐个 import → "MODULES OK"
BRIEF   : tool_instructions(kinds, brief=True)  443 字符；含 task(48)/ask(50)/command 含后台 op
FULL    : tool_instructions(kinds, brief=False) 5252 字符
ENUM    : DECISION.actions.kind 15 项 = GUIDES 14 项 + agent（agent 的说明在 AGENT_INSTRUCTIONS，不在 GUIDES）
BG      : start → status(state=running) → wait(exit_code=7, state=failed) → 重启后新实例 status 仍 exit_code=7
BG/gone : 把 pid 换成不存在的 → state='gone', exit_code=None,
          note="进程已经不在了，但没有留下退出码…所以它到底成没成功不知道"
TASK    : add×2 → repeat=True runs=2，meta 仍 1 行；list="…这件事做过 2 次"；未知 ref → KeyError
ASK     : ask→repeat=True(asked_times=2)→pending(waiting)→answer(1)→再 answer(0)→过期后 answered_late=True
OPFIELDS: OP_FIELDS vs AgentManager 七方法签名 → extra {}（today 之前修的那条仍然成立）
PROVIDER: 7 家档案齐；volcengine needs_manual_model=True models=[]；tencent list_models_supported=False
          catalog() 不带 key；resolve() 认不出就返回 (None, 原因)，不猜
```

---

## 3. 三处我认为还没过关的地方（按严重度）

### B-1【blocker】`ask` 的"问完怎么知道"这条链断在最后一厘米

- `GUIDES['ask']` 承诺 `current_state.questions`（`companion_context.py:31`），实际没有任何代码产出这个键。
- `PendingQuestions.context()` 写好了却没人调用（`companion_ask.py:196`）。
- `self._answered_question` 写了却没人读（`companion.py:2094-2100`）。
- 生产实测：18:13 提问被登记，18:14 被认领（`meta.pending_questions` 里 `answered=true`，`answer_text="保持一篇就行，别拆。你刚才问我的那个问题，现在还挂着吗？"`）。
- **后果**：她问完之后，下一轮**拿不到"哪个问题被答了、答的原文是什么"**——这正是这个工具声称要解决的问题。她只能靠读取 `meta` 或再 op=pending 才知道。
- **可判定判据**：下一轮上下文里出现 `current_state.questions` 且含 `id/question/answer`；或 `ask` 的回执里明说"这一轮回答的问题是哪条"。
- 修法（不代改，只给方向）：在 `_messages` 的状态里加 `questions = PendingQuestions(self).context()`；`_answered_question` 要么接进 `state['answered_question']`，要么删掉这个死变量。

### B-2【blocker】任务书第 4 组（"其他修复"）没有任何证据，且 `companion_agents.py` 自基线起零改动

- 见 §0.2。`return_code` 无条件改写、`memory`/`skill` 跨项目、结果轮阻塞 worker——**在云端都找不到对应改动**。
- 其中 `memory`/`skill` 跨项目那条尤其重要：`companion_tasks.py` 的整个设计前提（"她在 9 分钟里重做 8 次"）恰恰把它当成已修。
- **可判定判据**：`git show HEAD:life/companion_agents.py | grep -n "return_code"` 与基线逐字相同 → 若声称修了，属"声称 vs 实测"不符。

### B-3【blocker-】通道条目的判定与措辞用两份源，刚统一又分裂

- `companion_awareness.py:849-870`：`status_` 来自 `world_fact['state']`（新事实层），但 `reason=` 用 `channel_reason.get(state_)`、`usable=` 用 `state_ == 'connected'`（两个都是旧 `ACCOUNT_STATES`）。
- 这些键的 `channel_reason` 字典里**没有 `needs_setup`**（只有 `connected/disabled/unconfigured/login_required/disconnected/unknown`）——`disabled` 映射成 `needs_setup` 之后，`reason` 会落到"状态未知。"。
- 实测：生产当前 `im.channels.qq = {enabled:false, state:"disabled"}`（`/health` 与 `/rpc snapshot` 都是），事实层给 `needs_setup`；这条路径下 `reason` 具体写什么，取决于 `state_`（`disabled`）命中 `channel_reason['disabled']`——**这一处碰巧对**，但**"判定用新词、解释用旧词"的结构性分裂仍在**：一旦事实层与旧词表分歧（例如 `unknown` vs `unconfigured`），条目会自相矛盾。
- 注释里自己写着「以前这里判 `disabled` → offline，而 reason 文案说「不是掉线，是主动关的」——判定和措辞用的不是同一份事实，正是今晚反复出现的那个根因」（`:846-848`）——**修了一半**。

---

## 4. Blocker 列表（不能通过的项）

| ID | 项 | 判据 | 现状 |
|---|---|---|---|
| **B-1** | `ask` 的答案回看不到 | 下一轮上下文出现 `current_state.questions`；实测：`grep -rn 'current_state.questions' life/` 只命中 `companion_context.py:31` 这一行说明 | ❌ 不通过 |
| **B-2** | 第 4 组"其他修复"无改动无证据 | `git diff 178173d HEAD -- life/companion_agents.py` 为空；`git log --all \| grep -iE 'return_code\|跨项目\|worker'` 无结果 | ❌ 不通过（只能按"未做"记） |
| **B-3** | 通道条目判定/措辞两份源 | `companion_awareness.py:849-870` | ⚠️ 结构问题，当前数据下碰巧对 |
| **B-4** | `task` 的 `clear` 与她被告知的语义不符 | 实测（临时库）：标成 `done` → `list` 列出 1 条 → `clear()` 回执写「已完成的 1 条还在项目记录里（不删数据），**清单默认不列它们**」→ **再 `list` 仍是 1 条、状态仍是 `done`**。`companion_tasks.py:246-253` 不写任何东西，`list()` 不过滤 `done`。`GUIDES['task']` 同样承诺"默认不列" | ❌ 不通过（措辞误导，不是数据损坏） |
| **B-5** | md5 表已过期 | 报告 `5e76ba37`，实际工作区 `c9f0d399…`、HEAD `12047b02…`；`5e76ba37` = `2683968:life/companion.py` | ⚠️ 文档问题，必须修（这张表就是用来判"我跑的是不是这版"的） |
| **B-6** | `provider_tools` 改名后，文档里还有一处写旧名 | `providers.py:136` 注释仍写 `配置里 provider_tools=false`；实际读到的是 `cfg.model_supports_tools`（`companion_model.py:81`，`role_config.py:34`） | ❌ 不通过（照注释配置会**静默不生效**：配置里写 `provider_tools` 会被忽略，strict 照走） |
| **B-7** | 腾讯新地址认不出来 | `detect_by_base_url('https://tokenhub.tencentmaas.com/plan/v3')` → `None`；`_PREFIX` 里只有旧地址 `api.lkeap.cloud.tencent.com`（`providers.py:170-178`）。只写了新 `base_url` 的旧配置会得到 `resolve() → (None, 认不出…)`，然后按 `supports_tools=False` 走 **json_mode**——**更糟**：`base_url` 保留原样，但 strict 静默关闭，正是 P41 要修的那个退化 | ❌ 不通过 |

---

## 5. 「声称 vs 实测」对照表

| 声称（文档/提交/任务书） | 我实测的 | 判定 |
|---|---|---|
| P40 后台进程 8 项全 PASS | 我自己跑了 start/status/wait/logs/重启后 status/gone/未知 job 7 条路径，与描述一致；`reap_all` 存在并在 `executor.close()` 调用（`companion_executor.py:576-580`） | ✅ 成立 |
| 后台 `state=gone` 表示"不知道" | 实测 `state='gone', exit_code=None` + 明确 note | ✅ 成立 |
| P40 任务清单 10 项全 PASS、不新建表 | 我的隔离库复现 repeat/runs/单行；生产 `sqlite_master` 无新表 | ✅ 成立 |
| `task op=clear` 让已完成的默认不出现 | `clear()` 不写任何东西，`list()` 不过滤 `done` | ❌ 不成立 |
| P40-3 `ask` 进入模型上下文（A7 断言） | `PendingQuestions.context()` 无调用方；`current_state.questions` 不存在；`_answered_question` 是死变量 | ❌ 不成立（A7 若通过，测的是别的东西） |
| P40-3 "她下一句说什么，小鱼就当成答案" | 生产 18:13→18:14 确实认领成功（`meta` 有 `answer_text`） | ✅ 认领成立；❌ 回看不到 |
| P41 strict 判据改读 `supports_tools` | `companion_model.py:79-84` 确实改成 `schema is not None and supports_tools`，并用 `model_supports_tools` 覆盖 | ✅ 代码成立 |
| P41 修好了 opencode-go 下 19 次"没有可解析的 JSON" | 生产 `usage_calls` 18:00–18:28（修前）22 次、18:30–18:49（修后）11 次，**都没有 `usage=None` 的行**；无法从"用量字段"反推 strict 是否生效 | ⬜ **无直接证据**。要证，得看 `wire_schema_error` 审计或 `strict_client` 路径的请求体；否则只能说"代码看起来对" |
| P42 7 家硬编码档案 | 7 家齐、字段齐、`catalog()` 不带 key | ✅ 成立 |
| P42 腾讯新地址 | `_PREFIX` 未同步；只有新地址的配置会落到 json_mode | ❌ 不成立 |
| P42 多 tool_call 取第一个合法 + 审计 | `picked` 取**第一个能解析的**，不管名字（`companion_model.py:132-160`）；上层 `_commit` 在 `total>1` 时写审计（`companion.py:2437-2450`） | ✅ 代码成立；⚠️ 取"第一个"而非"名字是 respond 的那个"，若模型先说一句再补完整决定，会取到那句——这是设计选择，要她那边多一轮 |
| 任务书「`work` 的 `run` op（说明承诺了但没实现）」 | `_work_run` 里 `op == 'run'` 分支存在（`companion_executor.py:513-547`），读 `arguments.command`、没有就明说"run 需要在 arguments 里给 command"；但 `git diff 178173d HEAD` 里 **executor 的 +174 行主要给了 task/ask/background**——`run` 那一段**在基线里已有**（`git show HEAD:life/companion_executor.py:532-547`） | ⬜ 代码在，但**不是今天新做的**；"说明承诺了但没实现"这个说法与基线不符 |
| 任务书「`_work_run` 拿不到 command 就退回 goal」 | 当前代码**不退回 goal**（`:536-540` 直接报错）；但这段在基线里就是报错版 | ⬜ 今天没有改动它；不能算今天修好 |
| 任务书「`execute` 无条件把 return_code 改写成 0」 | `companion_agents.py` 自基线零改动 | ❌ 无改动 |
| 任务书「`memory`/`skill` 不能跨项目作证据」 | 同上，零改动；`companion_skills.py` 里只有一处 `evidence_scope='file_content'` | ❌ 无改动 |
| 任务书「结果轮认知阻塞 worker」 | 同上，零改动 | ❌ 无改动 |
| 腾讯 `supports_tools` | 档案写 `True`，注释自己承认"未实测（云端无 key）" | ⬜ 未测 |
| OpenAI 云端不可达 | 档案写 `supports_tools=True` + 2 个模型名 | ⬜ 未测 |
| `volcengine` 模型列表为空 | 实测 `models=[]`、`needs_manual_model=True`，`catalog()` 里 `has_models=false` | ✅ 陈述为真；⬜ 该家从未真实调用 |
| APK 未在真机测试 | 我没有复测；本轮改动里 `tools/build-companion-android.py` 有 2 行差异（`git diff 178173d HEAD`） | ⬜ 未验证 |
| P40 文档 md5 表 | 5/6 一致，`companion.py` 那条是提交时快照，已过期 | ⚠️ 见 §0.1 |

---

## 6. 一致性专项（任务书点名要查的三处）

| 概念 | 今天之前 | 今天新增的同类问题 | 判定 |
|---|---|---|---|
| `disabled` / `disconnected` / `offline` | 三词语义重叠 | `channel_fact()` 收敛成 `needs_setup`/`offline` 两词并附 detail，**是进步** | ✅ 改好了 |
| `queued` vs `pending` | agent 用 queued、别的用 pending | `ask` 用了第三套：`waiting`/`expired`；`task` 用 `todo/doing/blocked/done` 但对齐到项目记录 `active/waiting/paused/done`（`PROJECT_STATUS`）。**同一个"在等"在三个工具里三种词** | ⚠️ 新增 |
| `work_mode` | — | 顶层持久模式 `work_mode`（life/tool）与本轮用途 `tool_mode`（purpose=='work' 也算）**同名不同义**；她问"我是不是工具模式"时按持久值答会和实际不符 | ⚠️ 新增 |
| `ask` 的状态 | — | `pending` 里 `state` 只有 `waiting`/`expired`；撤掉的用 `answered=True + cancelled=True` 表示，`pending()` 看不到"撤过" | ⚠️ 新增 |

---

## 7. 我的独立判断：今天这批改动，能不能算"能力已经可用"

**不能算"这一类能力已经可用"，但里面有三块是可以用的。**

分开说：

1. **后台进程（#1）——可用。** 我自己跑通了全生命周期（含重启后、含 `state=gone` 这个"不知道"正例）。生产也在用（17:51 那个 job 退出码 0，她据此说出了"status 只返回运行状态，不返回输出"——这是她自己归纳出来的用法）。
2. **任务清单（#2）——能用，但有一句假话。** `repeat`/`runs` 的核心判据成立（我复现了）；`clear` 的语义与告知她的说法不一致（B-4）。
3. **结构化提问（#3）——登记和认领能用，"问完怎么知道"不能用。** 这是这个工具存在的**唯一理由**，而现在断在最后一步（B-1）。
4. **服务商档案（#4）——代码结构对，但两个会让它当场失效的洞**：腾讯新地址认不出（B-7）、旧配置项名 `provider_tools` 已被忽略而注释还教人这么写（B-6）。
5. **第 4 组"其他修复"——不在今天的改动里。**（B-2）

所以对任务书那句"能不能算能力已经可用"，我的回答是：

> **三个新工具里，一个真能用（后台），一个能用但说明有假话（任务清单），一个缺了它存在的理由（提问）；服务商这一层结构对但有会让它失效的配置洞；"其他修复"那一组今天没有发生。整体判「部分可用，不通过」。**

---

## 8. 我做了什么、没做什么

**做了**（全部只读 + 临时目录）：
- `git log` / `git diff 178173d HEAD` / `git show` 逐项比对；`md5sum` 复核报告里的表。
- 云端真源码上跑真模块：`BackgroundJobs`（临时 HOME）、`TaskList`（临时 sqlite）、`PendingQuestions`（假 core）、`providers`、`companion_context`、`companion_model` 整包 import。
- 生产 `life.db` **只读**打开（`mode=ro`）：`companion_actions`、`meta`、`companion_context_loads`、`usage_calls`、`companion_activity`、`sqlite_master`。
- `/health`、`/rpc snapshot` 带 `server-token` 读生产投影。

**没做**：
- 没改任何代码、没写生产库、没重启服务、没发 QQ。
- **没有对任何一家模型服务商发真实请求**（腾讯支持不支持 tools、OpenAI 可达性、volcengine 的模型名，一律只能按"未验证"记）。
- **没有复跑 P41 的"19 次没有可解析 JSON"**：生产 `usage_calls` 里 18:00 之后没有一条 `usage` 缺失的记录，拿这个反推 strict 是否生效不成立；要证需要 `wire_schema_error` 的审计留档，我没找到。
- **没有验证 `ask` 在真实模型一轮里是否真的读到了问题**（因为 `current_state.questions` 根本不存在，这一步注定失败；我用代码检索证明它不存在，而不是用一次对话"试试看"）。
