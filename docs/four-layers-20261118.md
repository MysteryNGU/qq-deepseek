# 四层之下：为什么她说「这就去查」却没做

日期：2026-11-18　执行：isolation-verifier
提交：`P50 audit`（见 `git log --oneline -- life/companion.py`）
**本文写的是一条完整因果链，以及追这条链时我自己错了四次。**

---

## 1. 症状

用户看到的是：

```
21:55:02  用户：你去 memory 里查一下那条「输出一行文字 hello-from-dsh」的子 agent 记录。它到底做成了没有？
21:56:13  小鱼：小鱼这就去 memory 里翻那条…翻到什么原话贴什么。
          ← 然后就没了

22:05:25  用户：同一句话又问了一遍
22:06:37  小鱼：小鱼去翻那条记录，翻到原话再回您。
          ← 然后又没了
```

**看起来像「承诺了不兑现」/「答应了但忘了」/「没有下一轮」。**

前两种判断都是错的，第三种也是错的（她确实有下一轮，只是那一轮什么也没做）。

---

## 2. 四层往下挖

### 第一层（表象）：她说「这就去查」
更新 / 它是**症状**，不是原因。

### 第二层：她到底决没决定要做？
`companion_activity(type='decision').details.content` 里存着决策原文：

```
[21:55:18] dialogue  actions=2  finish_reason=tool_calls
[21:56:13] dialogue  actions=2  projects=1  finish_reason=length
[22:05:40] dialogue  actions=1  finish_reason=tool_calls
[22:06:37] dialogue  actions=1  projects=1  finish_reason=length
```

**她决定了，而且动作参数是对的**（`kind=memory op=search`，`{"query":"hello-from-dsh"}`）。

### 第三层：动作为什么没生效？
`companion_actions` 里 **0 条**。

对今日全部 524 个带 action 的 decision 按 `finish_reason` 对账：

| finish_reason | decisions | 声明 action | 落库 action | 落库率 |
|---|---|---|---|---|
| tool_calls | 350 | 371 | 318 | 86% |
| stop | 169 | 190 | 187 | 98% |
| **length** | **5** | **8** | **0** | **0%** |

**`length` 这一档 8 个 action 全部丢失，100%。**

### 第四层：为什么 length 会把 action 全丢？
旧代码在 `finish_reason == 'length'` 时**整轮降级成纯回复**，actions 直接丢弃。

**但 JSON 其实是完整的。** 拿 21:56:13 那轮的原始 `content` 实测：

```
content 长度  : 1359
content 结尾  : '..."wake_after": 0, ... "scene": {"action": ""}}'   ← 完整闭合
json.loads    : 成功
keys          : 21 个
actions 数    : 2
```

**`finish_reason=length` 只说明「总输出撞到 token 上限」，不代表 JSON 被截断**——
前面可能有思考内容吃掉了额度，而 JSON 本身早就写完了。

### 结论：这已经是历史问题，不是现存 bug

```
P51 恢复代码（_recover_truncated_actions）上线：f11b97d  2026-11-18 22:12:15
两次截断（21:56:13 / 22:06:37）都早于 22:12
```

**用当前代码重跑那轮的原始 content，两个 action 都能被恢复（判据、schema 全部通过）。**

---

## 3. 这次追查我自己错了四次

按顺序，每一次都是被下一步的证据推翻的：

| # | 我当时的结论 | 错在哪 |
|---|---|---|
| 1 | 「23:09 没有下一轮」→ 其实是**我信了队长的转述** | 台账里有完整 5 条输出 |
| 2 | 「21:56/22:05 压根没生成 action」 | 决策里明明有 2 个 action |
| 3 | 「决策原文没落表」 | 找错了表：`decision_records` 是空的，原文在 `companion_activity.details.content` |
| 4 | 「三档统计 A=0，说明没问题」 | **口径答错了问题**：13/13「有动作」统计的是「决策里写了 action」，不回答「有没有落库」 |

**四次里有三次是「看了但看得不够」，一次是「信了没核的转述」。**

第 4 次最隐蔽：**数字是对的，但它回答的不是我们真正问的问题。**

---

## 4. 这条链回答了什么

**「她只说不做」不是性格问题、不是记忆问题、也不是调度问题。**
是**一行 early-return 在她说完话之后把动作丢掉了**，而那一行已经在 22:12 被修掉。

**今天剩下的 length 样本数为零。** 所以这是历史，不是现状。

---

## 5. 留下一个取证缺口（已修）

修复代码（P51）恢复了 action 之后，只写进 `state['truncated_recovered']`，
**读完即清**——事后查不到「今天救回过几次、救回了什么」。

后果：
- 无法统计 P51 到底救回了多少次
- 如果哪天恢复错了（把本该丢的半截计划派出去），事后无从追查
- 用户问「今天有几次被救回来」——答不出

**已补**：恢复发生时同时写一条 `companion_activity`：

```
输出被截断（finish_reason=length），但决定在截断前已经完整：恢复了 2 个行动
details = {recovered:[{kind,op,goal},…], purpose, finish_reason:'length',
           kind_tag:'truncated_recovered'}
```

实测七项全通过，包括**换一个连接重新查也能查到**（A6），
即：**当次能看到（state），历史也能查（activity）。**

**这条 audit 是取证工具，不是统计工具。**
下次真出现 length 而恢复失败，它是唯一的证据来源。

---

## 6. 仍然没有的东西

- **P51 上线后生产零样本。** 判据正确、链路完整、离线复现通过，
  但「线上真的触发过一次并且救回来了」——**没有证据**。
  这不能靠改代码获得，只能等下一次真实截断，届时上面的 audit 会留下痕迹。
- `tool_calls` 那档 86% 的 14% 缺失（53 个 action）**没有查**。
  样本里 `evt_cf9bcd15`、`evt_f1e0d5cc` 各出现两次，可能是另一个模式，
  但证据不足以定性。**记在这里，不做推断。**
