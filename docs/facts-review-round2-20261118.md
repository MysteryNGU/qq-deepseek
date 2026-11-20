# 评审第二轮：复核 7 个 blocker + 6 条查不到的现状 + 新规矩有没有被违反（P61 / t31）

评审者：reviewer（独立评审，不改代码）　时间：2026-11-18 22:46 – 23:25 CST
云端 `/opt/qq-deepseek`，`master`，HEAD = `cfffb47`，工作区只有本文件未跟踪
最高原则不变：**「她可以查也可以不查，根据她自己来判断。我们将世界所有事实摆在她的眼前。」**

**这一轮的判据只有两条**：① 她说的话和代码/台账对不对得上；② 摆没摆到她眼前。
**不采信转述**：下面每一格都是我自己读代码、自己跑模块、自己查只读台账得到的。

---

## 0. 先说一件方法上的事：今晚的改动，谁在什么时候改的

`git log --format='%h %ad %s' --date=%H:%M`（22 个提交里今晚 10 个）：

| 时间 | 提交 | 内容 |
|---|---|---|
| 21:56 | `4206e47` | verification 补终态 + 5 条回填 |
| 22:00 | `2c8cba2` | 截断丢弃的 action 留痕 |
| 22:02 | `4cdcc1e` | 事实消费时机改为请求成功后 |
| 22:12 | `f11b97d` | 截断时结构完整则恢复 actions |
| 22:19 | `cb6ac3d` | memory scope=errors + awareness 失败汇总 |
| 22:22 | `9383895` | 状态保存失败也带 reason + 289 条回填 |
| 22:27 | `9fd8316` | 子 agent 自述与系统判定分开 |
| 22:35 | `07a8c3a` | dsh 三层判据 + 修复被截断的 worker |
| 22:38 | `d90957e` | AGENT_INSTRUCTIONS 示例改 op+arguments |
| 22:41 | `54ac478` | 后台 op=list |

`git log --oneline -- <file>` 逐文件核过：**没有互相覆盖**。`companion.py` 被 5 次改动
（P50→P50b→P51→P53→P57），每次都只动自己那一段，符号数只增不减。
`companion_agents.py`（P49、P55）与 `companion_awareness.py`（P44、P52）同理。

**§0.13d 复核**（今晚唯一两次「整文件重写」的后果）：

```
178173d  agents=28 awareness=58      ← 基线
fc906bd  agents=28 awareness=58      ← 整文件重写，符号数没有减少
cb6ac3d  agents=28 awareness=59      ← 加 failure_summary
当前      agents=28 awareness=59      ← 一致
dsh_worker: def/class/__main__ = 6；__main__、_turn_error、classify_failure 全在；201 行
```

**结论：今晚没有发生「截断式改写丢掉符号」。§0.13d 这条规矩今晚是被遵守的。**

---

## 1. 七个 blocker 复核

| # | 我上轮的原结论 | 现在的状态 | 证据 | 判定 |
|---|---|---|---|---|
| **B-1** | `ask` 承诺 `current_state.questions`，代码不产出 | 仍产出，且**读不到时不写空数组** | `companion.py:2122-2132`：`state['current_state']['questions'] = PendingQuestions(self).questions_view()`；异常分支写 `{'error': '这一轮读不到提问记录'}`。`companion_ask.py:195-231` 给 id/question/asked_at/status/answer/answered_at/late/asked_times/options | ✅ **修好了，没回退** |
| **B-2** | 第 4 组「其他修复」没做 | `companion_agents.py` 现在有 `backfill_unverified`（`:1980`）、`_verify` 终态（`:1956-1976`）；`companion.py` 有 `_operation_status`（`:3733`） | `git log --oneline -- life/companion_agents.py` 显示 `4206e47`、`9fd8316` 两次改动；`_verify(completed,[])` 实测 → `'unverified'` | ✅ **做了**（但见 B-2b） |
| **B-2b** | —（新发现） | `backfill_unverified` **没有调用方**：`grep -rn backfill_unverified` 只命中定义处 | 但 5 条 `verification-reclassified` 事件在 22:05:16 已写入 → 说明当时用一次性脚本跑过，**代码里的函数是死代码**，下次再有存量不会自动补 | ⚠️ **修是修了，但入口没接上** |
| **B-3** | 通道条目判定与措辞两份源 | `reason` 现在按 `status_` 取，旧状态词只补充细节 | `companion_awareness.py:903-925`：`detail_by_state.get(state_) or channel_reason.get(status_)`；`channel_reason` 现有 `ready/needs_setup/offline/unknown/unsupported` 五个键 | ✅ **修好了** |
| **B-4** | `task clear` 说隐藏但没隐藏 | 真写 `hidden_at`，`list()` 默认过滤，`list(status='done')` 仍可取 | `companion_tasks.py:216-217`、`:259-273` | ✅ **修好了** |
| **B-5** | md5 表过期 | 报告改成「部署后现取」，但**写完又过期了**：6 条里 2 条已对不上（`companion.py` 809538c0→2b01888e、`companion_awareness.py` 113a9cd1→0063c1f5） | 见 §1.1 逐条重算 | ⚠️ **半修** |
| **B-6** | `providers.py` 注释教旧名 `provider_tools` | 全文已无 `provider_tools`，只有 `model_supports_tools` | `grep` 实测：`mentions provider_tools: False`；`model_supports_tools: True`（`companion_model.py:81`、`role_config.py:34`） | ✅ **修好了**（我上轮判据成立，队长那条「判据不成立」与我实测不符——见 §1.2） |
| **B-7** | 腾讯新地址认不出 | `_PREFIX` 加了 `tokenhub.tencentmaas.com`，并在 import 时跑 `_self_check()` | 实测 `detect_by_base_url('https://tokenhub.tencentmaas.com/plan/v3')` → `tencent`；**负向测试**：加一个假 provider → 立刻 `AssertionError: providers: '__probe__' 的 base_url … 认成了 None —— 把它加进 _PREFIX` | ✅ **修好了，而且自检是真能炸的** |

### 1.1 B-5 抽查结果（md5）—— 做法对了，但 6 条里 3 条又过期了

`docs/p44-blocker-fixup-20261118.md` 头一句写「**以下 md5 全部是部署后现取的**（`md5sum` 于本文写作时执行，非提交时快照）」。**方法对了**（比上一轮记提交时刻强）。我按当前文件重算：

| 文件 | 报告写的 | 现在实际 | 判定 |
|---|---|---|---|
| `life/companion_ask.py` | `7c4fd7db` | `7c4fd7db…` | ✅ |
| `life/companion_tasks.py` | `d567c3e2` | `d567c3e2…` | ✅ |
| `life/companion_remote.py` | `c9f010b7` | `c9f010b7…` | ✅ |
| `life/companion.py` | `809538c0` | **`2b01888e…`** | ❌ 写完之后又被 P50/P50b/P51/P53/P57 改了 5 次 |
| `life/providers.py` | `fdcd2f94` | `fdcd2f94…` | ✅ |
| `life/companion_awareness.py` | `113a9cd1` | **`0063c1f5…`** | ❌ P52 之后改过 |

> **结论**：`B-5` 判 **⚠️ 半修**。"部署后现取"确实执行了，但**表写完就不管了**——
> 同一天后面 6 个提交又改了其中 2 个文件，报告里的数字变成新的过期快照。
> 真正要的不是"写的那一刻是准的"，而是"读的那一刻能校验准不准"：
> 报告应当带上 `git rev-parse HEAD` 或文件 mtime，让读者能自己判断是否适用。

### 1.2 B-6 我要给自己纠一条

队长说我上轮「判据不成立」。我重新查了一遍：

- 上一轮我写的是「`providers.py:136` 注释仍教人写 `provider_tools`，照着配会静默不生效」。
- 当时（19:00 之后）`grep` 确实命中 `provider_tools`；现在文件里**一个都没有**。
- 所以准确说法是：**那是一个真问题、已经被修掉了**，不是判据错。

---

## 2. 6 条「存在但查不到」的现状

| 事实 | 上轮 | 现在 | 证据 | 判定 |
|---|---|---|---|---|
| 后台任务列表 | ❌ 没有 op=list | `OPS = (..., 'list')`；实测返回 `dict(count=2, jobs=[{job_id,state,started,exit_code,command,log}], evidence_note=...)` | `companion_background.py:83,163` | ✅ **修好了**（但见缺口 G-1） |
| 重启史 | ❌ 无入口 | **未做**。`_self_process_facts` 仍只有 unit/source_root/work_root/can_write_own_source/restart_to_take_effect/model/effect_note；`meta` 里 process/restart/boot/uptime 仍然全空 | `companion.py:1715-1729` | ❌ **仍是 0** |
| 今日错误汇总 | ❌ 无入口 | `failure_summary(core)` 已接进 awareness（`:1238` `failures=run('failures.summary', ...)`），并给 `how_to_look="memory op=search scope='errors'"`；`companion_memory._search_errors` 真实现 | 我对生产只读副本实跑：`total=143`，分组 8 类 | ✅ **修好了**（但见 G-2） |
| 本轮 token 用量 | ❌ 看不到 | **未做**。`companion_context_loads` 有 `request_tokens`，但 `_messages` 的状态里不出现 | `grep request_tokens life/companion.py` 无命中 | ❌ **仍是 0** |
| 子 agent 验证结论 | ⚠️ 永远 pending | 双管：`_verify` 终态 → `unverified`，且 `_summary` 现在写「子 agent 自己的说法（不是系统判定）」 | `companion_agents.py:1300-1320`；台账 5/5 已从 pending 变 `unverified`，5 条 `verification-reclassified` 审计事件 | ✅ **修好了** |
| 服务商可选 | ❌ 她看不到自己有哪些可选 | **未做**。`providers.catalog()` 只挂在 `/rpc providers`（桌面端），`companion_awareness.py` 里 `grep providers` **零命中**；`self_process.model` 只给「我现在用的这一家」 | `companion_remote.py` METHODS 里有 `'providers'`；awareness 不读它 | ❌ **仍是 0** |

**6 条现状统计：✅ 3（后台列表 / 错误汇总 / 验证结论），❌ 3（重启史 / token 用量 / 服务商可选）。**

---

## 3. 今晚 5 条新规矩，有没有被违反

### §0.11 改了「怎么写记录」之后，旧记录怎么办

| 实例 | 做法 | 我的核验 | 判定 |
|---|---|---|---|
| `verification=pending → unverified` | 带审计回填 | 5 条全改为 `unverified`；5 条 `verification-reclassified` 事件（22:05:16），正文写明「以前停在 pending，看起来像在验证；实际是验不了」 | ✅ **遵守** |
| `details={} → 加了 reason` | 带审计回填 | 289 条被补写；**回填后剩余空 details = 0**；审计一条「历史失败记录已补写「当时没记」：289 条」；原文案**不被覆盖**（只改 details） | ✅ **遵守** |
| `项目状态 → 旧的 cancelled 无原因` | **尚未处理** | 实测仍有 **22 条** `status='cancelled'` 且 `result` 里没有 reason | ❌ **未处理**（文档自己承认，但这是 §0.11 明确的第三例） |

**新发现的 §0.11 违反（G-2）：** 289 条回填写的是
「这条记录写于 2026-11-18 修复之前，失败原因当时没有记录（异常对象已不存在）」——
**对 289 条是对的**。但实测**还有 29 条** details 里既没有 `reason` 也没有 `error`
（例如 `{"action_id":"...","project":"dsh连通验证"}`），它们**没有出现在回填范围内**，
`failure_summary` 把其中 10 条归进「（这条没记原因）」，而 `_search_errors` 对它们给的是
「这条记录没有写原因（当时没记）」——**说真话，但没回填**。回填的选择条件是 `details='{}'`，
这 29 条 details 非空，所以漏了。

### §0.12 两个不同的东西被当成了同一个

| 检查点 | 结论 |
|---|---|
| `failure_summary` 的分组键 | ⚠️ **半违反**：`error + location` 优先，`reason` 只取首行前 80 字。实测把 22 条「回填写于修复之前」和 10 条「这条没记原因」分成两组——**这两组其实是同一件事**（都是"当时没记"），被拆成两类。反过来，不同原因但首行相同的会被合并。 |
| `_search_errors` 的 `reason` 兜底 | ✅ 正确：`error` 优先、`location` 附加，都没有时明写「当时没记」 |
| `_dsh_session_written` 的异常兜底 | ✅ **做得好**：查不了时 `return True` 让主判据（输出非空）继续起作用，并在注释里写明「查不了不等于没跑」 |
| `background.list` 的 `seconds: None` | ⚠️ 已完成任务的 `seconds` 永远是 `None`（实测两条都 `None`），而 `status()` 里算得出 `seconds`。**两个接口对同一事实给两种答案** |

### §0.13 云端改文件的唯一写法（assert count==1 + replace）

**无法从 commit 直接判定**（改写脚本不进仓库）。可判的间接证据：

- dsh worker 的 `__main__`、`_turn_error`、`classify_failure` **都在**（§0），说明那次修复没有重犯截断。
- 两次「整文件重写」（`fc906bd`、`09d91c8`）后符号数没有减少 → 即使用了截断式，也做了复核。
- **没有副本可查**：`docs/` 下没有留下改写脚本，**这条规矩本身不可审计**（G-3）。

### §0.13d 截断式改写后必须复核符号清单

✅ **遵守**（见 §0 的符号计数）。补充：`companion_dsh_worker.py` 201 行、6 个顶层符号，
与 §0.13 记录的"丢过一次 `__main__`"对照，现在的结构完整。

### §0.14 校验通过 ≠ 可用（schema 只证明结构合法）

**这是今晚做得最好的一条，也是最该表扬的一条：**

- `d90957e` 的提交信息直接写「实测 `inspect{agent_id}` 照字面执行会 `agent_id_required`」——**先跑再改**。
- 我复核：`AGENT_INSTRUCTIONS` 现在写 `arguments 写 {"agent_id":"…"}`；把里面的 **7 条示例**
  （inspect/wait/send/cancel/resume/read_artifact/create）逐个喂给 `OP_FIELDS` —— **全部零多余键**。
- 反例没有找到。**这条规矩今晚被遵守了。**

### §0.15 提到方法名之前，先 grep

| 抽查 | 结果 |
|---|---|
| `AgentManager._verify / _evidence_checks / _finish / _summary / _log / _row / backfill_unverified` | 全部存在 ✅ |
| `companion_awareness.failure_summary / _world_fact / capabilities / capability_summary` | 全部存在 ✅ |
| `backfill_unverified` 是否被调用 | **存在但零调用**（B-2b）——名字没错，但它是死代码 |
| `questions_view` 在 awareness 里？ | 不存在（我自己的探测脚本误写，不是产品代码） |

**结论：§0.15 在"名字对不对"上今晚没有违反；在"方法真的被接上"上有一个反例（B-2b）。**

---

## 4. 新发现的 blocker（本轮）

| ID | 问题 | 证据 | 为什么是 blocker |
|---|---|---|---|
| **G-1** | 后台 `op=list` 做了，但**没有任何说明告诉她有它** | `GUIDES_BRIEF['command']` 原文仍是「op=status/logs/wait/stop 查和停」——**没有 list**；`GUIDES['command']`（详细版）也**没有** `op="list"`（实测 `'list' in GUIDES['command'] → False`） | 这正是上一轮 B-1 的**同一类错误**：能力做了，说明里不写 = 她永远不知道。`§0.14` 的精神就是"照它做一遍"——照 BRIEF 做，她只会用四个 op |
| **G-2** | 回填范围漏了 29 条，`failure_summary` 把「回填写于修复之前」和「这条没记原因」拆成两类 | `SELECT details` 实测：29 条 details 非空但无 reason/error；`failure_summary` 分组里这两个 key 同时出现（22 条 / 10 条） | §0.11「绝不允许静默」+ §0.12「两个不同的东西被当成同一个」反向也成立：**同一个东西被当成了两个** |
| **G-3** | §0.13「云端改文件的唯一写法」**不可审计** | 改写脚本不在仓库、`docs/` 下无副本；只能靠 commit 结果反推 | 规矩要靠"事后能查"才成立；现在只能靠"看结果像不像丢过东西" |
| **G-4** | 「重启史」与「本轮 token 用量」**两条仍是 0** | `_self_process_facts` 无启动时间/重启次数；`grep request_tokens life/companion.py` 无命中 | 上一轮列为 ❌，今晚没有变化；`§0.11` 的第三例（22 条 cancelled 无原因）同样未处理 |
| **G-5** | `background.list` 与 `background.status` 对同一事实给两种答案 | `list()` 里 `seconds: None`（实测两条完成的任务都是 None），而 `status()` 算出真实秒数 | §0.12「两个不同的东西被当成同一个」的镜像：**同一个东西给了两个值**，她按哪个都说不出准数 |

---

## 5. 独立判断

**相对上一轮，今晚这 12 处改动是真修，不是文档层面。**

三条最硬的正例（我实跑过）：

1. **`providers._self_check()` 在 import 时真会炸**——我塞了一个假 provider，立刻 `AssertionError`。
   这比"测过"更可靠：**它以后每次启动都在测**。
2. **`_dsh_session_written` 查不了时返回 `True`**——把"查不到"挡在门外，让主判据继续起作用，
   并写明「查不了不等于没跑」。这是 §0.12 想说的那件事的正面写法。
3. **`AGENT_INSTRUCTIONS` 的 7 条示例逐条过 `OP_FIELDS` 零多余键**，且提交信息记录了"先跑再改"。

**但上一轮那类"做了却没摆到眼前"的问题又出现了一次**（G-1 后台 `op=list` 不在任何说明里），
而上一轮 6 条 ❌ 里**3 条至今是 0**（重启史 / token 用量 / 服务商可选），
外加 §0.11 第三例（22 条 cancelled 无原因）未处理。

> **判定：不通过（needs_revision）。**
> 理由不是"改错了"，而是**"摆到眼前"这一步只完成了 3/6，且新增了一个同类的漏写**。
> 按用户那句原则，**做出来但没告诉她的能力，等于没有**。

---

## 6. 我做了什么 / 没做什么

**做了**：`git log --oneline -- <file>` 逐文件核并发；`git show <commit> --stat` 核十处改动范围；
真源码上 import 14 个模块（零失败）；实跑 `providers._self_check()` 负向测试、`background.list`、
`_verify()`、`failure_summary()`（对生产只读副本）、`AGENT_INSTRUCTIONS` 七条示例对 `OP_FIELDS`；
生产 `life.db` **只读**查 `companion_agent_tasks` / `companion_activity` / `companion_agent_events` / `companion_actions` / `meta`。

**没做**：没改代码；没写生产库；没重启服务；**没对任何模型服务商发真实请求**；
没有复跑 dsh 真实链路（三层判据只做了代码阅读，未在真实 dsh 失败上验证 `failure_kind` 是否真出现）。
