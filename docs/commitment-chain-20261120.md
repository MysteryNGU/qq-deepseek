# 承诺链核对（P80，2026-11-20）

任务来源：用户亲自指定
> 挑最近 10 个小鱼说过"我去做／我派了"的真实对话，逐条对上动作、回执和最终交付，先修最常断的那一步。

**结论先行：这条链上最常断的一步，不是"说了没做"，也不是"说完成但文件不在"。
是「同一个事实存在两处，一处更新了另一处没更新，她读到哪处决定她说真话还是旧话」。**

---

## 一、先说方法：承诺句和动作怎么对上

任务说明给的关联方式（`companion_actions.source = event_id`）**对不上**——
实测：动作的 source 是 `companion_wake/self_observation`（729/736），
而她的承诺句在 `self_output/message`，两者永远不是同一条。

**真正的关联键是 `payload.turn_id`**：

```
self_output.payload.turn_id  ==  companion_actions.source
```

实测 82 条承诺句：`turn_id` 直连命中 52 条，兜底（同会话 ±5 秒）2 条，无法关联 28 条。

> 任务说明里写「她的输出是 `kind='message'`，注意不是 self_output，那个不存在」——
> **实测 self_output 存在**，7 天 3087 条，是她的常规输出通道。这一点需要更正。

---

## 二、10 条逐条对账

挑选标准：覆盖多种动作类型，全部为真实原文。

| # | event_id | 时间 | 动作类型 | 承诺原文（节选） | turn_id | 动作 status | 后续告知 | 交付证据 | 判定 |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `evt_bd95d16842fa9b7c40` | 11-20 01:41 | memory read | 就剩那张三栏表还没收尾，小鱼去看看它跑完没。 | `evt_74ea9c8c49d54881` | completed | `evt_e0070eb8` | result 内联产出 | **通** |
| 2 | `evt_c011c4701f315997ca` | 11-20 01:37 | command | 还剩两条 queued，小鱼去看看它们是刚排上的还是老账。 | `evt_dfa3c831f5024478` | completed | `evt_439e7057` | `return_code=0` + stdout | **通** |
| 3 | `evt_1bac3a5bde7106a0a5` | 11-20 01:35 | command | 小鱼现在看这 23 条到底什么原因。 | `evt_71101641772b49ca` | completed | `evt_9235fd2e` | `return_code=0` + stdout | **通** |
| 4 | `evt_411d85a88c31f13007` | 11-19 09:31 | web open | 这两个小鱼得亲自去看看。 | `evt_08170b352b954d53` | completed | `evt_de961dbe` | result 有 title + text | **通** |
| 5 | `evt_f3e0cdbf5f428816e5` | 11-19 00:58 | agent inspect | 我去看文件在不在，同时问它到底跑到哪一步了。 | `evt_12f877c3cb6749b9` | **failed** | `evt_0e1acfb1` | 无（动作失败） | **通**（失败也如实说了） |
| 6 | `evt_21732777b69c1e1705` | 11-19 00:56 | agent create | 重新派一次，这回指定 echo 节点。 | `evt_0315c67d19554532` | **cancelled** | `evt_ef5ff909` | 无 | **通**（取消也说了） |
| 7 | `evt_0685ae48a53982dae3` | 11-19 00:55 | memory search | 子 agent 已经派出去了，去 /tmp 建文件写 agent-test-ok。 | `evt_bc5ea60a36b746d3` | completed | `evt_f6a2d8b2` | result 内联 | **通** |
| 8 | `evt_6ed800b0db1a8db89f` | 11-18 16:31 | dsh | 这次让它写个小文件再读回来，成功与否都有回执。 | `evt_0431270d9f154137` | **failed** | `evt_3e42445c` | `return_code=1` + 错误原文 | **通**（失败也说了） |
| 9 | `evt_bab634a739864b2447` | 11-18 10:27 | phone | ws-16 还在，小鱼查一下它是不是还活着。 | `evt_a5d20ee4dc694de4` | completed | `evt_6a2eb56b` | output 为空（但 workspace_status 本来就没输出正文） | **通** |
| 10 | `evt_7b318b6c39cb564c6c` | 11-17 01:08 | town delegate | 小鱼去看看有没有人开了桌在等人。 | `evt_82bb1eb813314960` | completed | `evt_2e036f41` | delegation_id + phase=waiting | **通** |

**10 条全部通过。** 三样齐备：承诺原文可回查（event_id）、动作可回查（action_id + turn_id）、
交付可回查（result 内联产出 / return_code / 有后续告知）。

---

## 三、全量统计（不是只看 10 条）

把 7 天内 82 条承诺句全量判定，按"她说的是哪种话"分流：

```
类型      判定                                   条数
intent    通（有动作 + 有后续告知）                39
intent    A（台账无对应动作）                      15
claimed   通                                      8
claimed   A（台账无对应动作）                      15
claimed   C（说完成，动作实际 failed/cancelled）    5
```

**断点 A 一共 30 条，看着是最大断点。但进一步核实推翻了它：**

```
30 条"断点 A"中：
  同会话 ±10 分钟内有动作：28 条  ← 做了，只是不在同一个 turn
  完全没有动作：           2 条
```

那 2 条的原文：

```
evt_2f372080e78040ba  "哦……说得对，得小鱼去找您。"            （turn_id=None）
evt_8986aa2af6fd4144  "…卡在没有真手机和线，暂时没法往下试，先让它等着吧。"（turn_id=None）
```

**两条都是 turn_id 缺失 + 非动作性表述（情感/陈述），不是"承诺了没做"。**

**所以断点 A 不是真断点，是我的审计口径太窄（只认同一个 turn）。**

**断点 C 的 5 条，复查也全部是"动作失败/取消，但她如实告知了用户"——那不是断链，是失败被诚实报告。**

---

## 四、真正的断点：同一事实两处存储，一处更新一处不更新

顺着用户举的例子（"子 agent 说完成，但约定位置没有文件"）追下去：

### 4.1 交付文件层是健康的

```
全量检查 1610 条带 output_path 的动作：文件不存在 29 条（1.8%）
且 29 条全部是 Windows 路径（D:\projects\...），是历史环境的陈旧记录，不是当前问题
agent 类带路径的动作 7 条：报完成但产物不存在 0 条
```

### 4.2 但 agent 的核验结论两处不一致——12/12

```
companion_agent_tasks.verification   = 'unverified'   ← 真话（backfill 已处理）
companion_actions.result.verification = 'pending'      ← 旧话（写入时的快照，没跟更新）

12 条 agent 动作，12 条不一致（100%）
```

**代码里其实已经写好了正确的语义**（`life/companion_agents.py:11`）：

> `status=completed` 只说明子 agent 说做完了；`verification` 是另一列，
> `passed` 必须有真实证据。不核验就是 `unverified`/`pending`，不冒充通过。

**而且 `backfill_unverified()` 已经接进启动流程**（`life/companion.py:1188`），
台账那 7 条都已经从 `pending` 改成 `unverified` 了。

**问题在于：回执是快照，台账改了，快照没改。** 她若读回执，看到的就是"还在验证中"——
一个永远不会结束的等待。

### 4.3 她实际读哪处

```
life/companion_awareness.py:1061  → 读 companion_agent_tasks（真源，正确）
life/companion_awareness.py:1230  → 读 companion_actions（用于"做过哪些动作"的列表）
```

**能力判定读真源，所以"能不能用 agent"这一条她说的是真话。
但"我上次派的那件活核验了没有"这类回查，走的是 `companion_actions.result`，读到的是 `pending`。**

---

## 五、修了什么

**这一轮我没有改代码。**

理由：把交付层全部查完后，最常断的那一步**不成立**——

```
① 断点 A（说了没做）     → 28/30 是跨 turn，真断 2 条且都是 turn_id 缺失的非动作表述
② 断点 C（说完成没做成） → 5 条全是动作失败但如实告知，不是断链
③ 断点 E（说完成没交付） → 交付层 1.8% 文件缺失，全是历史 Windows 路径；agent 类 0 条
```

**用户要修的是"最常断的那一步"。数据显示这一步不存在于"承诺→动作→交付"主链上，
只存在于"同一事实两处存储"这个次生层。**

**而这个次生层的修法需要先定一件事，我没有权决定：**

```
companion_actions.result 是「写入时的回执快照」，还是「随时可回查的当前状态」？

  若是快照 → 现在就是对的，只要在展示时标明"这是当时的回执，当前状态请查台账"
  若是当前状态 → 需要让回执跟随台账更新，或改成查询时 join

两者的改动方向相反。定错了会引入新的不一致。
```

---

## 六、建议的修法（等确认后执行）

**方案 A（小改动，推荐）：展示时标明快照。**
```
在 awareness 展示 agent 回执时，若 result.verification 与台账当前值不同，
显示为「当时核验状态 pending；当前 unverified」——两个都摆出来，不说哪个是错的
改动点：life/companion_awareness.py 里取 agent 回执的那一段
不碰数据，不碰状态机
```

**方案 B（数据层）：让回执跟随台账。**
```
backfill_unverified 与 verify() 更新台账时，一并更新对应 companion_actions.result
风险：result 是历史回执，改它等于改历史；且需要按 agent_id 反查 action，成本高
```

**我倾向 A。B 会让"历史回执"变成"可变的当前状态"，那是另一种失真。**

---

## 七、重跑对比

**修完前（本次）：**

```
10 条抽样：10 通过 / 0 断
82 条全量：归因后真断 2 条（都是 turn_id 缺失的非动作表述）
两处一致性：companion_agent_tasks vs companion_actions.result  → 12/12 不一致
```

**修完后需重跑：**
```
① 同样 82 条判定，真断条数应保持 2 或更低
② 12 条不一致应变为 0（方案 A：展示层同时给出两个值；方案 B：数据层归一）
③ 新增检查：任何"verification 两处不一致"都应能被脚本发现（不能静默）
```

---

## 八、附：任务说明里三处需要更正的事实

```
① 「她的输出是 kind='message'，注意不是 self_output，那个不存在」
   → self_output 存在，7 天 3087 条，是她的常规输出通道

② 「companion_actions.source = 承诺句所在 event」
   → 不成立。source 是 companion_wake/self_observation（那一轮的触发事件），
     承诺句在 self_output。两者用 payload.turn_id 关联。

③ 「agent 的 27 次 7/27 不能解释为 27 个任务只完成 7 个」——这条用户说得对，实测拆分：
   create: failed 7 / cancelled 5 / completed 3
   inspect: failed 5 / completed 4 / cancelled 2
   tools:   failed 1
   → 真正"派活成功"的只有 create/completed = 3 次
   → 而 verification 全部 pending（台账 unverified），即"做完了但没核验过"
```
