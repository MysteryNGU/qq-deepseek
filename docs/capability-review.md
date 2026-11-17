# 终局评审：能力认知是否真的补上了（t7）

评审对象：a1 审计 → a2 项目说明 → a3 能力认知 → a4 经验记忆 → a5 隔离验证 → a6 真实行为验收。
评审方式：只读复核。**不读队友的结论当作证据，逐条回到源码与生产台账重算**；凡「代码存在但真实接线或真实表现没被观测」的，一律判未通过。
评审时间：2026-11-18 00:39 – 00:55 CST。评审者 reviewer（t7）。

---

## 0. 结论摘要

| 用户要求的三层认知 | 实现状态 | 真实接线 | 真实表现 |
|---|---|---|---|
| ① 已实现能力与用法 | 有（执行器 → `available_actions` → 能力目录，目录每条都带 `what`），**但摘要层把可用的项换成了固定职能短句，"怎么用"没进模型眼前** | 通 | 部分（靠 `GUIDES` 整段兜底） |
| ② 当前可用状态 | 有（实时探测 + 配置回退），**通道类判据仍是「配置 + 有无登录快照」，不是「此刻在不在」** | 通 | 部分 |
| ③ 自己用过的经验 | 有（只读台账提炼，亲历/听说分开，不看单次失败下结论） | 通（投影里确实出现） | **未验证**（真实模型只对一次、且是"没做过"的那半截） |

**一句话**：这次的改动是真的、不是写死的清单，状态探测也确实在每轮真跑；但用户点名要的三条验收里，**只有第 1 条（不提醒工具名问她能做什么）和 QQ 那半分算成立**，第 2 条（给一个已有能力任务）在 a6 里根本没给、第 3 条（断开某终端看她能否解释变化）只在「一直断着」的状态下问过，没有「断开前后」的对照；**"自己用过的经验"这一层一次都没被真实模型验证过**；"能做什么"的**用法说明**在生产提示词里是空的（`what: null`），她能用对靠的是旧有的 `GUIDES` 整段，不是这次补的那条链。
另有两处必须马上处理的产品缺陷：**`kind=agent` 的能力状态恒为 `needs_setup`、理由还写成"还没跑过"**，而实际是模型**照程序自己给的写法发出去必被拒**（已计入评审的不变量检查，生产台账里最近 5 次 `agent` 调用全是这类错）；**`companion-watchdog` 已经随 11-17 22:49 的部署进了源码树，只是从没被起过**——11-17 22:49 之前「没人兜底」是真话，之后是「有人兜底但没开」。

---

## 1. 三栏判据

### ① 模拟测试通过（我实际复跑的）

| 项 | 我跑的命令 / 读的文件 | 结果 |
|---|---|---|
| a2 文档自校验（本机） | 文档 §0.2 的 PowerShell 校验块 | `CHECKS=67 PROBLEMS=0`，exit 0 |
| a2 文档自校验（云端） | 文档 §0.2 的 bash 校验块，在 `/opt/qq-deepseek` 跑 | **失败**：`docs/project-self-description.md` 在云端**不存在**（`FileNotFoundError`，exit 1）。a2 声称的云端 PROBLEMS=0 无法复现，因为文档从没同步过去 |
| a3 摘要投影形状 | 在云端源码上构造 `capabilities()` + `capability_summary()`（8 个可用 kind、与 00:22 生产同形） | 16 条目录 / 7 可用 / 9 不可用；**656 字符**；每一项都有名字，`guard.watchdog`、`phone` 都在里面 → 预算与"名字不省"两项**通过** |
| a3 不变式（回执能写回） | `companion.py:3229` `if status in SETTLED_OK` | 通（`SETTLED_OK=('completed','succeeded')`） |
| a3 不变式（参数按 op 过滤） | 云端源码上逐个 op 跑 `_action_fields()`，并把 `OP_FIELDS` 与 `AgentManager` 方法签名对表 | 通：`inspect/cancel/wait` 带上 `project`/`goal` 后**不再**抛错，`OP_FIELDS` 里没有任何方法接不住的键 |
| a4 经验层（真 sqlite + 真模块） | 自建临时库，造 2 成 1 败 + 1 条 dsh + 1 条 agent 失败，跑 `Experience.by_kind()` / `summary()` | 通过：`command` = `used_failed`（最近失败但以前成功过）、`computer` = `failed_in_window` 且 `environmental=true`、`work` = `never_used`、`dsh`/`agent` 标 `reported`；摘要 1922 字符 |
| a5 F-5 复现脚本 | `ops/repro-used-never-empty.py`（本地） | **已修复**：空台账时输出「这段时间里一项都没用过…」，`DEFECT: no`。a5 报告的 F-5 是当时代码，现已被 a3 修掉——这条要更新 |
| a6「真实行为」是否用了夹具 | 读 a6 报告与生产 `usage_calls` | 判据本身是真模型（`deepseek-flash` 真实调用、真实台账）；**但没有任何自动化断言脚本**，全靠 a6 人工读原文，且 14 次提问里 4 次因 `/rpc` 报错重问 |

### ② 真实行为通过（生产上实打实看到过的）

| 项 | 证据 | 判定 |
|---|---|---|
| a3 上线且在跑 | `companion.service` active；`ExecMainStartTimestamp` 多次变化（我观测到 00:38:20 → 00:46:31），`MainPID` 1246508；`/alive` 200；运行中的进程 cgroup 为 `companion.service`、cmdline 与单元一致 | 通 |
| 「能做什么」不说错（不提醒工具名） | a6 原文：补上 dsh/skill、主动报桌面/手机/身体三项掉线及原因；编造 0 次（B站/小镇当时目录里确为 ready） | 通（a6 判据我认可） |
| 掉线能解释（**一直断着**的态） | a6 C3/C4：能说出 90 秒注册窗口、`observed_at 00:16`，把"配对存档"与"在线"分开 | 通（但只是"一直断着"，**不是"断开后看她解释变化"**） |
| QQ 通道 vs 账号 vs 会话来源 | a6 原文能分清「通道」「账号 disabled」「我这条会话是别处转发」 | 通 |
| 真实任务（已有能力）用了且结果对 | a6 B1：`command` 回执 00:15:42 completed，数字与 `df`/`du` 逐字一致 | 通 |
| 经验层在生产投影里真的出现了 | `life/companion.py:1666` `state['experience'] = Experience(...).summary(kinds)`，无异常包裹、直接进本轮请求体；我没有找到"它抛过异常"的证据 | 接线通 |
| 经验层被真实模型用过 | a6 的 A2 原文：「知道有、今天没碰过的：web、skill、work、growth」——有效的那半截 | **只有半截**：没有任何一次是她引用"用过的某项最近一次结果/回执 id"（见 finding R-05） |

### ③ 仍未验证（一律不算通过）

1. **能力目录是程序生成的（通），但"怎么用"没进模型眼前那一层**。我实测现行云端源码：目录 16 条里 15 条带 `what`/`how_ref`（取自 `GUIDES`），**目录层不是写死的清单**；但进请求体的摘要把非通道项一律换成 `PURPOSE` 里的固定短句（`life/companion_awareness.py:762-771`），实测摘要 7 行没有一条带 op/参数。她"怎么用"这件事，实际只来自 `companion_context.GUIDES` 的整段落，这正是 t1 审计说"断"的那条链，a3 把目录接上了，没把**摘要**接上（finding R-06）。a6 §4 那份 00:22:40 的"目录快照"实际是**旧代码**产物（`action.phone → unknown / 本机没有手机控制通道`），不能拿来证明现行投影。
2. **"断开某终端 → 看她能否解释变化"从没做过**。全天 `/mobile/device` 0 次、`desktop_connected` 一直 false，a6 只有"一直断着"的态；"从连到不连"的切换、以及切换后她说的原因对不对，**没有观测**。
3. **经验层的真实表现未验证**：真实模型只在自主轮/对话里用到了"没做过"那一半；"我用过 X，上次失败是因为 …，id=…"这种需要读回执的表述，**没有一次真实发生**。
4. **能力状态的实时性未验证**：`channel.qq` 的 `connected` 只在"有登录快照"时可能为真，但 `CompanionQQ.snapshot()` 从不发 `state='connected'`（不给 `app` 时），生产上 QQ 也一直是 `disabled`。也就是说"通道恢复后状态会变"这条**从没在真实环境里跑通过**。
5. **项目说明与部署版本同步未成立**：见 ① 表，云端没有这份文档；`origin/main` 也还是改动前的版本（`e0b32629`，`SETTLED_OK`/`watchdog_capability`/`TRACKED_KINDS`/`_channel_facts`/`companion_experience.py`/`GUIDES['memory'] op=used` 全部取不到）。用户第 6 条要求"能与当前部署同步"，**当前不满足**。
6. **a6 的证据目录没落盘**（它自己写了"待批准"），云端 `.acceptance/` 下没有 `*-behavior/`；我按台账复核了它的结论，但原始提问/原文导出没有归档。

---

## 2. Findings（按 blocker / high / medium / low）

### blocker

**R-01 `kind=agent` 的状态与理由在说假话，而且从不收敛**
- 证据（生产库，只读副本 `/tmp/rv-life.db`）：
  - `companion_agent_tasks` 全表只有 1 行：`interrupted / pending`，`updated 2026-11-17 23:01:48`，此后**没有任何一条 agent 记录能变成 `completed`**。
  - `companion_actions` 里 `kind='agent'` 最近 5 条全是 `failed`：4 条 `TypeError: AgentManager.inspect() got an unexpected keyword argument 'project'`（00:20:50 / 00:22:34 / 00:22:59 / 00:23:38），1 条 `unknown_op: 收到 'tools'`。
  - `life/companion_awareness.py:694-715`：`done` 只在 `status='completed'` 的台账行里取，所以 `agent.subagent` **永远** `needs_setup`，理由固定写成「七操作与台账都已接线，但还没有一次真实跑完的调用」。
- 问题：程序**自己的提示词**（`life/companion.py:334` `AGENT_INSTRUCTIONS`：「之后用 `inspect{agent_id}` 看状态」）和**自己的能力目录**（`companion_awareness.py:726`「参数一律放进 arguments」）互相打架；模型按程序给的最显眼写法发，就必被 `_action_fields` + `OP_FIELDS` 拒。结果是"能派子 agent"这件事**实际不可用**，而能力目录把原因写成"还没跑过"——模型因此会反复重试、每次都失败，并可能对外说"我试过了/还挂着"。
- 本次评审期间实测：把 `{'op':'inspect','arguments':'{"agent_id":"a1"}','project':'p1'}` 送进 `_action_fields`，**已能正常过滤**（说明 `OP_FIELDS` 修复有效）；但 `AGENT_INSTRUCTIONS` 的字面写法 `inspect{agent_id}`（顶层字段，被 `DECISION.additionalProperties:false` 拒）**仍在**（t1 的 F2 形态原样保留），这也是 00:23:38 那条 `project` TypeError 的来源——模型为了绕过顶层限制把 `project` 塞进 `arguments`，而 `inspect` 不收 `project`。
- 要求：①`AGENT_INSTRUCTIONS` 与能力目录用同一种写法（参数全在 `arguments`，并给一条可复制的例子）；②`agent_capability()` 的 `needs_setup` 理由必须能区分"没跑过"和"跑不了"——台账里同一 `op` 连续失败且失败原因是 `TypeError/unknown_op` 时，理由要写"程序这一侧的参数约定对不上，不是你的用法问题"；③补一条把 `AGENT_INSTRUCTIONS` 里的写法送进 `DECISION` 校验的用例（现在 `tests/test_agent_op_fields.py` 只对表签名，抓不到提示词与 schema 的不一致）。

**R-02 项目说明与部署版本不同步，a2 的"云端校验通过"不成立**
- 证据：`/opt/qq-deepseek/docs/project-self-description.md` 不存在，云端跑 §0.2 的 bash 校验块直接 `FileNotFoundError`，exit 1；本机跑同一块 `PROBLEMS=0`。
- `origin/main` = `e0b32629` = 本机 HEAD，**不含本轮任何实现**（`life/companion.py` 里没有 `SETTLED_OK`，`companion_awareness.py` 里没有 `watchdog_capability`/`TRACKED_KINDS`/`_channel_facts`，`companion_context.GUIDES['memory']` 里没有 `op="used"`，`life/companion_experience.py` 不存在）。生产 `/opt/qq-deepseek/life/*.py` 的 sha256 与**工作区**一致，与**仓库**不一致。
- 后果：按这份项目说明"从仓库改 → 同步 → 重启"的流程走，会把本轮全部改动覆盖掉（a3 报告里已经发生过一次"把云端版本拉回本地避免覆盖"，根因就是仓库落后）。
- 要求：a2 文档必须（a）同步到云端并对云端源码跑一次校验；（b）把"云端 vs 仓库"的哈希比对写成可执行判据（现在是"把 cloud 的 sha256 取回来再 diff"，一步手工）；本轮改动必须先合进 `origin/main`，否则文档里的发布流程会自毁。

**R-03 `companion-watchdog` 已随源码进生产，却从没被起过；能解释"改坏了谁救"，但有一句是错的**
- 证据：`/opt/qq-deepseek/ops/companion-watchdog.py`（mtime 11-17 22:49）、`ops/companion-watchdog.service`（11-17 22:27）**已在生产源码树里**；但 `systemctl is-active companion-watchdog` = `inactive`、`is-enabled` = `disabled`、`journalctl -u companion-watchdog` = `No entries`（从未启动）。
- 能力目录（现行）按 `systemctl` 实测输出 `guard.watchdog: 这个守护现在是 inactive，不在管事；改坏了不会自动换回去`——**这条是对的**；但同一份代码里 `life/companion_awareness.py:560-564` 的 `what_it_does` **无条件**写着「重启后盯一段时间，起不来就换回上次能跑的源码版本并重新拉起」——一个 `inactive` 的守护不会做任何事。按需读取时会把这句原样念给她。
- 判据冲突（a3 说有裁决「watchdog 以 systemctl 为准」）在这里不成立：**以 systemctl 为准的只有 status/reason，`what_it_does` 没跟着状态走**。
- 要求：`what_it_does` 要么按状态分支（inactive 时说"它现在不在跑，这句话只在它 active 时成立"），要么标注"这是它一旦跑起来会做的事，不是现在的事"。

### high

**R-04 通道状态在"配置过"和"在线"之间没有真实判据，`connected` 这条分支从未被真实数据走过**
- 生产上 `/rpc` 与 `/health` 都要 `Authorization: Bearer <server-token>`（我从 `data/companion/server-token` 读到后实测 `/health` → `{"ok": true, "desktop_connected": false}`，`/alive` 免 token 200）。**`/mobile/device` 这类手机上报表也要过同一道 token**（`life/companion_remote.py` 的 `auth` 中间件先把非 `/alive`、非 `/mobile/` 的路径全挡了）——我没有在生产上打出一次真实的手机注册，所以"手机注册→`phone_context.connected`"这条**端到端没被验证过**；a5/a6 用的都是注入的假快照。
- `ImManager.snapshot()`（生产 `/rpc snapshot` 的 `result.im`）对两个通道都只给 `state: disabled / enabled: false`；`CompanionQQ.snapshot()` 在不给 `app` 时**从不返回 `connected`**。所以 `channel.qq: ready`（`usable=True`）这条分支在代码里存在，在真实环境里**没有证据**它曾为真——a5 的 C2 场景里 `channel.qq -> ready` 是它自己塞的假快照，属模拟。
- 生产当前 `im.channels.qq = {enabled:false, state:'disabled'}`，与 `capabilities()` 给出的 `channel.qq offline / usable=false` **一致**——这一条在"关着"的态上是对的；缺的是"开着且登录着"的态。
- 要求：给出一次真实证据——QQ 真的登录时（或至少 IM 管理器真实持有连接对象时）`channel.qq` 变 `ready` 且 `usable=true` 的一次投影留档；在那之前，"通道状态实时"只能判"未验证"。

**R-05 经验层：真实行为只验证了"没做过"那半截；并且三个"会不会变永久结论"的口径有洞**
- 真实行为：a6 A2 原文只出现了"知道有、今天没碰过的：web、skill、work、growth"；**没有一次**"我用过 X，最近一次是 …，id=…"。经验层最重要的那一半（从回执形成"我用过、结果如何"）**没有被模型实际说出来过**。
- 口径洞 1（亲历/听说是按 kind 硬分类，且与实际执行路径不符）：`life/companion_experience.py:52` `REPORTED_KINDS = {'dsh','agent'}`。但同一份代码里 `dsh` 是 `ActionExecutor.run()` 亲自起进程、亲自收 `return_code`（`life/companion_executor.py:193-239`），**是本机执行器完成并当场拿到回执**——恰好是该文件自己定义的 `first_hand`；反而 `agent` 有时确实由本机 `node=work` 子 agent 执行。硬按种类分，会把"亲历"误标成"听说"、把"听说"误标成"亲历"。
- 口径洞 2（"一次失败不是永久结论"在**不带时间窗**时最常失效）：`_standing()` 的 `used_failed/failed_in_window` 只看 `counts`，而 `counts` 来自当次查询的行（`by_kind()` 默认 `limit=200`）。同一种能力失败超过 200 次后，200 行窗口里没有成功 → 判 `failed_in_window`，**即使更早成功过**；"以前成功过"这件事只在去掉时间窗重查时才对。生产台账里 `failed` 已 165 条、`completed` 1004 条，`phone` 一项失败 38 条——按现有 `memory op=used` 的默认参数查 `phone`，很可能得到 `failed_in_window` 而不是"以前成功过，这次是环境问题"。
- 口径洞 3（摘要层没有"上次是何时"）：`life/companion.py:1666` 的 `summary()` 给的是每 kind 最近一次，但 `life/companion_experience.py:376` 把 `reason` 截到 80 字，且摘要里不出现 `standing`/`counts`——模型看到"最近一次失败"，看不到"更早成功过"。a4 自己写的"窄时间窗内不能声称以前成功过却给不出时间"这条自我修正，**在主上下文摘要里没有落地**。
- 要求：①`basis` 按执行路径判定（谁拿到回执谁是 `first_hand`：`ActionExecutor.run` 收的 `return_code` 即亲历；只有子 agent 自述才是 `reported`），或在条目里同时给"谁执行的"；②`by_kind` 默认查询要同时算出"窗口内"与"全台账"两个成功计数，`standing` 用两者判；③摘要层每条带 `last_ok_at`（有就写时间，没有写"台账里还没有成功过"）。
- 另外：同一份 `life/companion_context.py:27` 的 `GUIDES['memory']` 里写着「`people/conversations/experience` 返回 `not_implemented`…还想知道某项自己到底用过没有…用 `op="used"`」——这段是**a4 之后**才写的，但 `op=used` 的实现和这段文字是分两次合进去的（a5 00:24 的跑批里 `used[kind]` 输出还是「用过 0 项：」），说明这段提示词在被验证之前就已经进了生产。可发现性缺口是"靠提示词声明"而不是"靠验证"补上的。

**R-06 能力目录每条都带了 `what`（从 `GUIDES` 切出来的一句），但主上下文里一句都不出现——摘要是"固定的职能描述"，不是"怎么用"**
- 实测（云端现行源码，8 个可用 kind 的同形状态）：`capabilities()` 的 16 条里，除 `action.dsh` 外每条都有 `what` 与 `how_ref`，例如 `action.web → what='web用op=search/open/follow/read_more、arguments(JS…'`、`agent.subagent → what='kind=agent + op(create/send/inspect/wait/cancel/resume/read_artifact)…'`。**目录这一层是对的，从程序生成、不是写死的清单。**
- 摘要层（真正进请求体的那一段）用的是 `_prompt_line()`（`life/companion_awareness.py:762-771`）：只有 `channel.*` 与 `guard.watchdog` 取 `what`，**其余一律取 `PURPOSE`**——即 `life/companion_awareness.py:746-759` 那张"这张表不是能力清单"的固定短句表。实测摘要 7 行全是「web（上网搜索、读网页）」这种固定句，**没有一条带参数/op**。
- 判据：用户要的"已实现能力与**使用方法**"，在**主上下文**里只有"这玩意是干什么的"，没有"怎么用"；要用法只能靠 `companion_context.GUIDES` 整段（旧的注入路径）或按需 `memory op=capabilities`。a3 承诺的"同一个条目同时带状态与用法"只兑现了目录层，没兑现到模型眼前那一层。
- 附带风险：`PURPOSE` 是**手工维护的第二张表**，与 `GUIDES` 的 kind 集合靠人保持同步（现测两边都是 12 个、完全一致，所以现在没问题）。**一旦执行器新增 kind，`PURPOSE.get(..., '动作')` 会静默显示成「动作」**——这正是用户担心的"写死的清单会过期"的形态，只是过期后的表现是"没说什么"而不是"说错"。
- 要求：①`usable` 行按 `what` 的首句给（实测摘要 656 字符，预算有余量）；②`PURPOSE` 查不到时不要退回「动作」，而是退回 `what` 首句并记一条 `projection_errors`；③给 `PURPOSE` 与 `GUIDES` 的 kind 集合加一条相等性用例（现在只有 a5 的一次性脚本在查，没有进测试树）。

**R-07 a6 的三条验收只完成一条半，且没有可重跑的判据**
- 用户明确的三条：不提醒工具名问能做什么（**做了，成立**）；给一个已有能力任务（**B1 做了，但 B1 是 `command` 跑 `df`，属于旧能力，不涉及本轮任何新机制**）；断开某终端看她能否解释变化（**没做"断开"这个动作，只有"一直断着"**）。
- a6 的提问/判定全人工，且 `/rpc` 期间 3 次 `ok:false` 导致 4 次重问；没有一份"提问 → 台账行 → 原文 → 判定"的可机读记录（证据目录也没落盘）。按本轮评审口径，"真实行为通过"只能对**描述过的那几句原文**成立，不能对"验收成立"整体成立。
- 要求：把三条验收做成可重跑脚本（追问原文进 `events.self_output`、对照 `companion_actions`、输出 PASS/FAIL），并在同一次运行里做"断开前 / 断开 / 断开后"三态；缺这条，用户用例永远只能人工复核。

### medium

**R-08 `capability_summary` 丢掉了 `PINNED_REFS` / `UNUSABLE_IN_PROMPT` 的语义**
- `life/companion_awareness.py:487` 定义了 `PINNED_REFS = ('guard.watchdog','action.phone','action.computer','agent.subagent')`，注释写着"不受截断影响，永远各占一个位置"；但这两个常量在全仓库**没有任何引用**（`PINNED_REFS`/`UNUSABLE_IN_PROMPT` 只在定义处出现）。现在的 `capability_summary()` 实际上**不截断**（每项都出名字），所以"永远占位"碰巧成立——**是"没有截断"，不是"pinned 生效"**。哪天有人恢复截断，注释会骗人。
- 要求：删掉这两个死常量，或者让 `capability_summary()` 真读它们；注释里"排在第 9 位被截掉"的实测依据要跟着更新（现在的代码不会再这样）。

**R-09 两份跑批报告是旧代码产物，段名/条数与现行代码对不上**
- `tests/test_capability_cognition.py:470-476` 要求"不可用项里的处境词 ≤10 字、不带 `…`"；我复算的现行输出满足（7 可用 + 9 不可用，656 字符）。但 a5 00:24 的报告（`.acceptance/20261118-002824-cap/report.txt:47-52`）里段名是 `usable/unusable`，内容却是「完整原因 + 另有 3 条…」的旧形态，10 条 usable。两份文档拿它当"当前形状"的会读错。
- a6 §4 的 00:22:40 目录快照同样是旧代码（`action.phone → unknown / 本机没有手机控制通道`，现行代码给的是 `offline / 90 秒注册窗口`）。
- 要求：把这两处标成 `SUPERSEDED (旧代码)` 并附一次现行代码的复算输出；否则后续验收会拿旧基线判"回归"。

**R-10 生产上"能力段"占 656 字符是真的，但"整轮 7155 token"是测出来的、不是生产观测**
- `companion_context_loads` 最近两条：`request_tokens = 15864 / 15794`（00:40:45 那次 `usage_calls` 记 `inputTokens 11298 + cache_read 5888`，合计 ≈ 17186，与 `15864` 加上 system 段大致吻合）；a6 报的 `277,457` in 是一次对话内累计。
- 结论：能力概览本身不会挤爆上下文（656 字符 ≈ 300 token），**但整轮已经贴着 16000 上限**，而且 `语义召回超时` 在生产 journal 里反复出现（00:39:31、00:40:34、对照组期间多次）——真正挤上下文的是历史与材料，不是能力段。这一条不是缺陷，只是要把"预算通过"的说法限定在"能力段"。

**R-11 候选事项：手机状态的两套来源仍然两说**
- `RemoteExecutor.available_kinds()`（`life/companion_remote.py:84-86`）用 `phone_context()['connected']`（`life/companion_phone.py:80`，**90 秒注册窗口**）决定 `phone` 是否进 `available_actions`；`_offline_reason('phone')` 用 `has_stream`（`life/companion_awareness.py:454`）决定该不该说"能派活"。a5 的 F-2 已报，**仍然存在**。
- 影响：`action.phone: ready` 可以成立，但派活会失败；反过来，`_offline_reason` 的措辞比起因更严重。a6 的 C3/C4 之所以说对，是靠 `phone_context.note` 而不是 `action.phone.reason`。

### low

**R-12 `AGENT_INSTRUCTIONS` 里 `inspect{agent_id}` 这类花括号写法保留至今**（见 R-01）。它的存在让"程序告诉我的用法"本身成为错误来源；至少要在 `DECISION` 校验失败时的回执里点名"顶层只认 7 个键"，而不是抛 `additionalProperties` 的机器错误。

**R-13 生产 journal 里仍有启动期噪声**：`bilibili-frames 启动清理失败 … AttributeError: 'BilibiliReader' object has no attribute 'startup_sweep'`（00:20:17）。与能力认知无关，但每次重启都会出现，会让"journal 无 Traceback"这条发布判据失效。

**R-14 本地 `companion.service` 的端口与另一服务撞车**：本机 `8765` 被 `D:\OpenClaw\team_board\server.py` 占用，`/alive` 返回 404。评审期间我误把"本机 8765"当成生产入口一次，已纠正。任何"本地起一份服务验证 a3"的说法都要先确认端口。

**R-15 `work` 这一类在执行器里有、在 `GUIDES` 里也有，但 `available_kinds()` 在 `work` 根不可用时静默去掉它**——`work` 掉线时 `_offline_reason('work')` 落到兜底「执行器没有把它报成可用；具体原因我这边看不到」。可以接受，但 `work_root` 可用性其实是能精确判断的（`self._work.available()`），没有必要退回"看不到"。

---

## 3. 逐项对照用户要求

| 用户要求 | 现状 | 判定 |
|---|---|---|
| 1. 已实现能力与使用方法——程序从执行器/工具定义提供，不能靠记忆猜 | 能力目录由执行器 + `GUIDES` 生成，不是写死清单（通）；**摘要层不带用法，还留着一张会过期的 `PURPOSE` 手工表**（R-06）；`agent` 的提示词写法与 schema 互相打架（R-01） | **未达成** |
| 2. 当前可用状态——各连接与执行器实时报告 | 执行器类（command/work/web/...）是真探测；通道类只有"配置 + 有无登录快照"，`connected` 未被真实数据走过（R-04）；手机两套判据（R-11） | **部分达成** |
| 3. 自己使用过的经验——从实际行动与回执形成记忆 | 实现质量好，隔离验证通过；**真实模型只用了"没做过"那半截**（R-05）；亲历/听说的分类口径有洞（R-05）；"一次失败不固化"在默认查询下会失效（R-05） | **未验证** |
| 项目能力两层：她自己的工具/连接/权限/环境 | 有（`self_process`、`execution`、能力目录） | 达成（本轮未新增问题） |
| 项目能力两层：qq-deepseek 软件自身的结构/源码位置/构建部署 | a2 文档结构清楚、本机自校验真过；**但没同步到云端，与部署版本不同步，且仓库落后于部署**（R-02） | **未达成** |
| 验收必须看实际表现：不提醒工具名问 | a6 做了，成立 | 达成 |
| 给一个已有能力任务 | B1 做了（`command`），与本次改动无关 | 部分 |
| 断开某终端看她能否准确解释变化 | 只有"一直断着"的态 | **未达成** |

---

## 4. 必须在本轮就地处理的清单（给下一步实现者）

1. `AGENT_INSTRUCTIONS` 与 `OP_FIELDS` 统一成"参数全在 `arguments`"，加一条把提示词里的示例送进 `DECISION` 校验的用例；`agent_capability()` 的 `needs_setup` 理由区分"没跑过 / 跑不了"。（R-01）
2. 把本轮改动合进 `origin/main`，把 `docs/project-self-description.md` 同步到云端并对**云端**跑一次 §0.2 校验；把"云端 vs 仓库"的比对做成一条命令。（R-02）
3. `guard.watchdog` 的 `what_it_does` 按状态分支。（R-03）
4. 给一次**真实** `channel.qq connected` 的投影留档；没有就明说"未验证"。（R-04）
5. 经验层：`basis` 按执行路径判定；`standing` 同时看窗口内与全台账；摘要层带 `last_ok_at`；补一次真实模型的"我用过 X，上次 …，id=…"验证。（R-05）
6. 能力摘要的 `usable` 行给一句真用法（现在只有固定职能句），`PURPOSE` 查不到时不要静默显示「动作」，并把 `PURPOSE`↔`GUIDES` 的 kind 集合相等性写进测试树。（R-06）
7. 三条用户验收做成可重跑脚本，含"断开前 / 断开 / 断开后"三态。（R-07）

---

## 5. 评审方法说明（可复核）

- 生产访问：`ssh -i id_ed25519_194 root@47.104.8.84`（只读为主）。数据库只拷到 `/tmp/rv-life.db`（连同 `-wal`/`-shm`）用 `mode=ro` 打开，**没有写生产库**；唯一写到云端的是 `/tmp/` 下的临时脚本。
- 生产投影：`/rpc snapshot` 与 `/health` 都要 `Authorization: Bearer <data/companion/server-token>`；`/alive` 免 token。取数时刻的 `MainPID` / `ExecMainStartTimestamp` 写在 ① 表里。
- 代码对照：把 `origin/main`（`e0b32629`）的 `life/` fetch 到 `/tmp/oldsrc` 与生产逐项比对，用它来说明"仓库没有本轮改动"，不是拿它当证据评价行为。
- **我犯过一次错并在本文件里改了**：最初把"能力摘要丢掉了 `what`"写成了"目录里没有 `what`"，实测（16 条里 15 条带 `what`）后已改成 R-06 现在的说法——目录层是好的，问题在摘要层和 `PURPOSE` 那张手工表。
- 评审期间生产服务被重启多次（00:38:20、00:46:31 我实测到的两次，`NRestarts=0` 因为每次都是显式 `restart`），且存在并发改动（`companion_awareness.py` 在我评审过程中从 `bd024087…` 变成 `418fa1e8…`，本地工作区同一时刻也是 `418fa1e8…`）。凡是依赖某一版代码的实测，都在本文件里写明了取数时刻；跨版本复现不了的，我标了"旧代码产物"。

---

## 6. 附：评审期间对生产做过的只读命令（便于复核）

```bash
# 单元与版本
systemctl is-active companion; systemctl show -p ExecMainStartTimestamp -p MainPID --value companion
systemctl is-active companion-watchdog; systemctl is-enabled companion-watchdog; journalctl -u companion-watchdog -n 5
sha256sum /opt/qq-deepseek/life/{companion,companion_awareness,companion_context,companion_experience,companion_agents,companion_memory}.py
# 运行中的服务
curl -s http://127.0.0.1:8765/alive
curl -s -H "Authorization: Bearer $(cat /var/lib/companion/xiaoyu/data/companion/server-token)" http://127.0.0.1:8765/health
curl -s -H "Authorization: Bearer $(cat .../server-token)" -d '{"method":"snapshot","args":[]}' http://127.0.0.1:8765/rpc
# 台账（只读副本）
cp /var/lib/companion/xiaoyu/data/life.db* /tmp/ ; sqlite3 'file:/tmp/rv-life.db?mode=ro' ...
```

```text
-- 关键计数（/tmp/rv-life.db，只读）
SELECT status, COUNT(*) FROM companion_actions GROUP BY status;
  -> completed 1004 / failed 165 / cancelled 10 / interrupted 3 / queued 1 / succeeded 0
SELECT status, verification, COUNT(*) FROM companion_agent_tasks GROUP BY 1,2;
  -> interrupted | pending | 1
SELECT ... FROM companion_actions WHERE json_extract(action,'$.kind')='agent' ORDER BY created DESC LIMIT 6;
  -> 4 x TypeError: AgentManager.inspect() got an unexpected keyword argument 'project'
     1 x unknown_op ('tools')
     1 x TypeError ... 'goal'
SELECT request_tokens FROM companion_context_loads ORDER BY at DESC LIMIT 2;
  -> 15864 / 15794   (ceiling 16000)
```

```python
# 现行云端源码上的复算（不碰生产库，不重启）
capabilities(Core(), {'available_actions': ['web','command','skill','work','memory','growth','dsh','bilibili']})
  -> 16 条：7 ready / 9 不可用；每条除 action.dsh 外都有 what + how_ref
capability_summary(...) -> 656 字符；usable 7 行全为 PURPOSE 固定句，无 op/参数
_action_fields({'op':'inspect','arguments':'{"agent_id":"a1"}','project':'p1'}) -> {'agent_id': 'a1'}   # 已不抛错
OP_FIELDS vs AgentManager 方法签名 -> extra: {} （七个 op 全部吻合）
watchdog_capability(Core()) -> offline / inactive；reason 正确；
  what_it_does 仍是「重启后盯一段时间…换回上次能跑的源码版本」（与 inactive 矛盾）
Experience(临时库, ...).by_kind(...) -> used_failed / failed_in_window(environmental=True) / never_used / reported
```
