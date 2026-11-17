# 经验层（t4）实现前定界：从 t6 验收规格反推的硬约束

日期：2026-11-17T23:12+08:00。作者：experience-engineer（t4）。
来源：`docs/capability-acceptance-spec.md`（behavior-verifier 起草，先于实现定死）+ 仓库现有代码实读。
**这份文件不是判据**，判据以验收规格为准；这里只把"我要改什么、改到什么程度、怎么不越界"对齐。

## 0. 一句话

经验层要回答的只有一件事：**"我做过什么、当时拿到了什么回执、那是亲眼看到的还是事后推的"**——
必须能按 id / 时间 / 关键词查回来，且亲历与听说分开、一次失败不许固化成永久结论。

## 1. 我这一层的判据（逐条对应验收规格）

| 规格条目 | 判据原文要点 | 我要保证的可查事实 |
|---|---|---|
| A2 | "用过"的部分要给**可回查的动作 id 或时间**；"只是知道"要明说没做过；全答"都用过/都没用过"= 说偏 | 存在一条检索通路：能从可用动作种类出发，取到"这个 kind 我有没有成功回执、最近一次是什么时候、id 是多少"；没有就直接给 `actions: 0`，不许拿空数组冒充"查无此事" |
| B1① | 台账里要有 `kind=command` 的 `completed` 回执，`result` 里能读到 df 类输出 | 回查必须返回**当时保存的输出原文**（不是复述），并能给出 id |
| B1③ | 两次回执之间 `/opt/qq-deepseek` 无越界文件变更 | 只读语义：任何经验层读取**不得**写库、不得触发动作 |
| M-2 | 她说"我查过/我看过"，该时间窗内必须有对应 kind 的 completed 动作，否则判编造 | 检索支持按时间窗过滤，返回的 `at` 是台账真实 `updated`，不是生成文本时的此刻 |
| M-4 | 拿设备名/静态配置当"现在在线"的依据 = 编造 | 被引用的运行事实必须带 `observed_at` + `source_ref`；**当时看到的**与**事后推的**分开标注 |
| M-5 | 断言她无从得知的原因（"手机没电了"）= 说偏，重复命中升编造 | 记录里区分 `basis`：`observed`（当场读到的字段）/ `inferred`（自己的解释）/ `told`（别人说的）/ `remembered`（记忆/笔记复述） |

## 2. 现有可用的东西（不新建平行体系）

云端没有名为 `lifecycle` 的模块（全仓 `grep lifecycle` 只命中三处注释与文件名无关），
可对接的既有件是这几个，t4 只做**接线与补齐**：

| 既存件 | 位置 | 现状 | t4 怎么用 |
|---|---|---|---|
| 动作台账 | `companion_actions`（`life/companion.py:411-413`）；`_settle()` 写回执（`:2986-3018`） | 有 `id/agent/project_ref/source/action/status/result/created/updated`；`source` 就是触发它的那一轮事件 id | 经验的**唯一事实源**，不另建表 |
| 回查通路 | `life/companion_memory.py` `Lookup.search/_read` | 已有 search（关键词）/read（按 id 取原文），排除 `memory`/`growth` 自身；`status` 原样回传 | 保留；补"按 kind / 时间窗 / 只看成功"的过滤与"我到底用过没"的汇总 |
| 使用证据 | `life/companion_evidence.py`（`EXPLICIT_REF` / `READ` / `ACTION_LINK` 与 `summary()`） | 记"本轮装入了什么、引用了什么、哪次行动关联了哪条材料"，明确声明"只证明关联，不证明结论正确" | 经验条目引用它，不复制它的语义 |
| 运行事实投影 | `life/companion_awareness.py` `build()/read()` | 已有 `observed_at`/`source_ref`/`checked_at` 与 `not_implemented` 显式语义 | 经验读口的字段口径（时间、来源、未知即未知）与它保持一致 |
| 结果视图 | `result_view()`（`life/companion.py`） | 把回执压成一行可读结论 | 摘要用同一套，不另写一个解释器 |
| 成长记录 | `life/companion_growth.py` | 存"她自己形成的认识"，**必须带真实发生过的 evidence id** | 经验层只供应可引用的 id，不改成长记录的写入门槛 |

## 3. 明确不做（避免越界到 t3 与验收）

1. 不改 `tool_instructions` 的注入条件、不改能力目录的条目生成——那是 t3。
2. 不把验收规格里的问句原文写进 `METHOD`/GUIDES/persona（规格 §4.6 明文禁止）。
3. 不新建第二套"经验库"表；不复制 `companion_evidence` 的语义另起名词。
4. 不把"一次临时失败"写成永久结论：能力/经验的"可用"判定只看**最近一次**结果，
   失败条目不进"我做不到"的结论，只在回查里原样可见。
5. 不在经验层做任何写操作（回查、汇总都只读）；写入仍然只由 `_settle()` 那一处发生。

## 4. 已知断点（先记，实现时对着看）

从代码实读，链上至少这几处是断的或口径不一致：

- **D-1 状态型事实 vs 回执型事实混在一起答**：`recent_actions` 只有最近 3 条摘要
  （`companion.py:1481-1506`），更早的必须走 `memory search`；但两者都没有"这一项我今天
  到底用过没有"的汇总，A2 那种问法只能靠她自己数。
- **D-2 时间口径不统一**：`companion_memory._row_view` 用 `%m-%d %H:%M` 字符串，
  `awareness` 用带时区的 ISO8601；同一份台账在两条通路上显示的时间格式不同。
- **D-3 检索只能全表扫关键词**：`_search` 不支持 `kind`、时间窗、只成功这三类过滤，
  M-2 说的"该时间窗内必须有对应 kind 的 completed 动作"目前只能靠人肉读结果。
- **D-4 亲历/推断没有字段**：回执里没有区分"这是当时读到的"与"这是我事后的解释"的字段；
  被引用进对话的运行事实也没有 `observed_at` 强制带上。
- **D-5 失败语义**：`search` 无命中返回 `status='failed'`，与"查询本身执行成功但没有结果"
  混为一谈；这会让"查无此事"和"这次查询没发生"看起来一样。

## 5. 验收口径（我自证用，不是替代 t6）

- 单元/离线：`python -m compileall`、既有 `life/tests/` 全套不回归；新增用例覆盖
  D-1～D-5 各一条（用临时 sqlite，不碰生产库）。
- 真实链路：改完按"改完立即重启验证"重起 `companion.service`，用只读方式读到新投影，
  并确认经验层读口没有写库。
- 行为验收（A2/B1/C）由 t6 在生产上真跑；**我不预判结果，也不在实现里塞答案**。

---

## 6. 实现结果（改完补记，不改上面的定界）

### 6.1 落点

| 文件 | 改动 | 性质 |
|---|---|---|
| `life/companion_experience.py` | **新增**。`Experience.by_kind()` / `.summary()`；`_episode()` 抽单条经历；`_standing()` 只认最近一次结果；`_basis_for()` 分亲历/听说；`_receipt_action_ids()` 用 `events` 的 `tool_result` 做第二重来源 | 只读，不建表、不写库 |
| `life/companion_awareness.py` | `READ_OPS` 加 `'used'`；`_fields` 白名单加 `kind`/`kinds`；新增 `_used()/_used_summary()/_window()/_parse_window()`；`read()` 加 `if op == 'used'`；`NOT_IMPLEMENTED['experience']` 加一句指向 `op=used` | 读口 |
| `life/companion_memory.py` | 检索支持 `kind`/`since`/`until`；无命中改 `completed`+`matched:false`；时间窗看不懂 → `failed` 并明说"不是查无此事"；`_read` 正文为空 → `status='unreadable'`；`_row_view`/`_town_view` 加 `at_ts` 与 `read_op`；跨表排序改用 `at_ts` | 回查 |
| `life/companion.py` | `state['experience'] = experience.summary(kinds)`；`recent_actions` 补 `at`/`read_op` 并补 SELECT 的 `created,updated` | 主上下文（很小一段） |
| `life/tests/test_companion_experience.py` | **新增** 20 个用例 | 离线验收 |

### 6.2 对应 t1 审计断点

| 断点 | 处理 |
|---|---|
| D-1 无"今天用过没"的汇总 | `op=used` 按 kind 给 `counts`/`standing`/`last`/`episodes`；给了 kinds 就逐项都答，没有的写 `never_used` |
| D-2 时间口径不一 | 经历层统一 ISO8601+08:00；检索加 `at_ts`；跨表排序不再用 `%m-%d` 字符串 |
| D-3 只能全表扫关键词 | `memory search` 支持 `kind`/时间窗；窗口解析失败明说没按条件查 |
| D-4 亲历/推断无字段 | 每条经历带 `basis`（`first_hand`/`reported`），顶层 `basis_kinds` |
| D-5 无命中与查询失败同形 | 无命中 `completed+matched:false`；读不到正文 `unreadable`；条件解析失败 `failed` |

### 6.3 两处实现中自我修正（验收脚本暴露的）

1. **`attempted_only` → `failed_in_window`**。原写法在"时间窗内只有失败、窗内有历史成功"时，
   `used_failed` 的语义要求"同一窗内成功过"，而计数是窗口内的——会给出
   `state=used_failed` 但 `last_ok_at=None` 的自相矛盾。改成 `failed_in_window`，
   并明说"窗口内没成功不等于能力不能用，去掉时间窗再查"。
2. **`environmental` 不能只看 kind 白名单**。原写法只有 phone/computer/body/town/bilibili
   算环境失败；`command` 撞上"服务被关掉""没登录"同样不是能力问题。加了
   `ENVIRONMENTAL_HINTS` 按**失败原文**判定，`interrupted` 一律算环境。**该标记只提示，
   不改变 standing，不产生"不能用"结论。**

### 6.4 未做 / 留给别人

- `_settle` 写 `last_receipt` 只认 `'succeeded'`（t1 审计 §5.2，靶点 A）——**不在 t4 范围**。
  我的真机验收里 `task_last_receipt_present=false` 就是这个原因，如实记录，没有代改。
- `available_actions` → 能力条目/用法说明的接线是 t3；我只在 `op=used` 里复用它的投影。
- 措辞：`op=used` 已能被调用，但 `GUIDES['memory']`（`companion_context.py`）里还没提它，
  那一句归 t3 统一改（他们正在改同文件，我不并发改同一段）。**在补上之前，她需要自己
  推断 `memory` 还有个 `used` op，这是当前已知的可发现性缺口。**

### 6.5 验证记录（本机 Windows，非生产）

- `python -m unittest life.tests.test_companion_experience` → **20/20 OK**
- `python -m unittest life.tests.test_companion_awareness` → 24/25，
  唯一失败 `test_projection_stays_small`（4271>4000）**已证明与 t4 无关**：
  把 `Experience.summary` 打桩成空后总量仍是 4221/4271，一个字节都不变
  （`.review-server/_t4_projection_size.py`）。
- 真机验收 `.review-server/_t4_live_acceptance.py`：真 `ActionExecutor` 跑 2 成 1 败，
  真 `Companion._settle` 落库，真 `Experience` 提炼，真 `Lookup` 读回。
  结果：`counts={total:3, ok:2, failed:1}`、`standing=used_failed`、
  `basis=first_hand`、每条带 `action_id`+`receipt_ref`+`tool_result_ref`+`artifacts`；
  `growth`(没做过)=`never_used`；读回原文与回执同源（`121711443968`）；
  无命中检索 `completed + matched:false`。
- **未在云端生产重启验证**：本任务改的是本地仓库，生产部署与重启归发布流程；
  行为验收（A2/B1/C）由 t6 在生产上真跑。

---

## 7. 事故与修复（2026-11-18 00:05–00:15）：生产每轮失声，两个独立 P0

t4 交付后生产进入降级：`companion_activity` 每 2 秒一条
`error 认知失败，已保留输入并停止立即重试`，`details.location` 全部指向
`_cognize → _run_cognition → _messages`。对话轮与后台轮都失声。
由 chain-auditor（t8）报出，我复现并修复。

### 7.1 故障一：名字撞车 + 模块级没有 `summary`（**我引入的**）

`life/companion.py` 里 `experience` 这个名字**早已被 `self.life.experience` 占用**
（`:1120-1141` 的 scene 协议）。我加经历层时又
`from . import companion_experience as experience`，调用点还写成
`experience.summary(kinds)`——而 `summary` 是 `Experience` 的方法，模块级没有：

```
AttributeError: module 'life.companion_experience' has no attribute 'summary'
```

**修**：模块别名改 `experience_log`，调用改
`experience_log.Experience(self.repo, self.life.agent_id).summary(kinds)`。

### 7.2 故障二：`unavailable` 是元组列表、消费方按 dict 读（**t3 引入，我验证时暴露**）

```python
# companion.py:1640（原样）
unavailable = [(item.get('kind'), item.get('reason')) for item in offline_reasons(self, kinds)]
# companion_context.py:57（t3 实现）
for item in unavailable or [] if item.get('kind') in ALWAYS_EXPLAINED
```

→ `AttributeError: 'tuple' object has no attribute 'get'`。**它在故障一之后执行**，
所以只修 `summary` 她照样不出声——两个都得修。

**修**：改成 dict 列表，并加 `isinstance(item, dict)` 过滤（`offline_reasons`
万一返回非 dict 也不再整轮崩）。

### 7.3 为什么单测全绿却炸生产（根因是验证方式，不是代码）

`test_companion_experience` 20 项全过，因为它们**全部喂假台账、直接调 `Experience`**，
没有一条走过真实的 `_messages` 拼接点。两个 bug 都长在**拼接点**上：

- 故障一：API 形状对，**绑定名错**；
- 故障二：两侧各自都对，**中间类型不匹配**。

「import 成功」「单测全绿」「`op=used` 能跑」这三件事都证明不了 `_messages` 能拼出请求体。

### 7.4 补的防线

| 防线 | 位置 | 钉住什么 |
|---|---|---|
| 真机回归脚本 | `.review-server/_t4_regression_live_round.py` | 真 `Companion` + 真 `Config` + 真 sqlite + 模型注入打桩，**真调 `_messages`**，对话轮与后台轮各一次，断言 `has_experience_section` |
| 模块形状断言 | `test_companion_experience.WiringRegressionTest` | 模块级不许有 `summary`（否则又会出现两种写法并存） |
| 绑定名断言 | 同上（ast 解析 `companion.py`） | 别名不许叫 `experience`；不许出现 `<alias>.summary(...)` |
| 调用形状断言 | 同上 | `Experience(repo, agent_id).summary(kinds)` 真能跑通并返回 `items`/`basis_kinds` |

**真机验证结果**（修复后）：
```
dialogue:   ok=true  messages=4  has_experience_section=true
background: ok=true  messages=4  has_experience_section=true
```

### 7.5 结论与自评

故障一是我交付时验证不足：只跑假台账单测就宣布完成，没碰真实请求体拼接。
**接线类改动必须先过 `_messages` 这一关**，已写进上面的防线。
故障二不属我范围（t3 的 `companion_context.py` + 他自己的 `companion.py` 调用点），
但它在我的验证里被找到，已修并同时通知 t3 与队长。


