# P47 agent 修复：四个问题、两个根因，以及一次没有执行的指令

日期：2026-11-18　执行：isolation-verifier
提交：`fc906bd`　`life/companion.py 50f9e1ed`　`life/companion_agents.py 565d2ceb`
服务：`active` / `NRestarts=0` / `alive 200` / err 0

**这份文档主要写「怎么判断的」，不是「修了什么」。**

---

## 1. 四个问题，两个根因

队长给的失败台账看起来是四件事：

```
① 幂等命中被标 failed/cancelled
② AgentManager.inspect() got an unexpected keyword argument
③ status=completed 与 outcome=incomplete 并存
④ 任务建了但永远不跑完
```

实测后归并成两个：

| 根因 | 覆盖 | 判据 |
|---|---|---|
| **A. 台账把「子任务状态」当成了「本次操作成败」** | ① ③（以及新发现的一条） | 同一句「同一个请求已经登记过」，台账随队列满不满分别读成 completed / failed / cancelled |
| **B. 从来没有超时机制** | ④ | `lease_until` 字段**写了 5 处、读了 0 处**；01:01 建的任务 19 小时后仍是 `queued` |
| **② 已修，不再复现** | ② | 连续 `inspect` 5 次、参数各不相同，`unexpected-keyword = 0` |

**② 的处置方式值得单说**：它已在上一个版本修好，我没有为了交差去改一段本来正确的代码。
判据是「现在还发不发生」，不是「代码里有没有这段」。

---

## 2. 我拒绝了队长给的方案，因为我先查了一件事

### 指令
> `SETTLED_OK = ('completed', 'succeeded', 'queued')` —— 同意，排队不是失败。
> 但要求：`queued` 要在 `_settle` 里保持 `result.status` 原样，**别让「不是失败」变成「是成功」**。

### 查证
`SETTLED_OK` 全仓库**只有一个读点**（`companion.py:3653`），用途是：

```python
if status in SETTLED_OK:
    # 成功回执本身就是进度事实 … 记一条 last_receipt 当进度
```

**它回答的是「这件事算不算进度」，不是「这次操作是不是失败」。**
把 `queued` 加进去 = 把「排上队」记成「做成了」——**正是同一句指令后半段警告的那件事。**

### 真正的病灶在写端
```python
def _settle(self, row, result):
    status = result.get('status', 'failed')      # agent 的 result.status 是子任务状态
    UPDATE companion_actions SET status=? …      # 直接落库
```

### 修法
只翻译**台账那一列**，回执一个字节不动：

```python
ACCEPTED_TASK_STATUS = ('queued', 'running', 'pending')   # 任务在跑 ≠ 操作失败
def _operation_status(self, kind, result):
    if kind == 'agent' and status in ACCEPTED_TASK_STATUS:
        return 'completed'
    return status
```

实测：
```
create→queued（队列有空）   task=queued   ledger=completed
create→queued（幂等重放）   task=queued   ledger=completed
create→running             task=running  ledger=completed
create→failed/cancelled    ledger=failed / cancelled（照旧）
其它 kind 一律原样透传
回执 result.status 仍为 queued
```

**「不是失败」没有变成「是成功」，只是不再被记成失败。**

---

## 3. 新问题的结论：**本来就没污染，所以不加字段**

队长要求：失败的 `inspect` 不许改任务状态，必要时记独立字段 `last_inspect_error`。

实测（拿生产最新一条任务做靶子，打三次失败 inspect）：

```
未知 agent_id      → AgentError(unknown_agent)        拦在写入之前
空 agent_id        → AgentError(agent_id_required)    拦在写入之前
错误类型 agent_id  → AgentError(unknown_agent)        拦在写入之前

任务 status：completed → completed（未变）
全表：无任何一条被翻动
```

**结论：不需要加 `last_inspect_error`。** 因为根因不是「inspect 写了任务状态」，
而是**台账把「这次查询失败」和「那个 agent 失败」写进了同一个 `status` 列**——
这正是根因 A。A 修完之后，agent 行只记操作成败，任务状态回 `inspect()` 里读。

加一个新字段是**对同一个病灶的第二次修复**，会让两处判定再次分叉。

---

## 4. 其余两处

**④ 排队超时**：按生产真实等待量级（39s / 134s / 458s）取 1800 秒，超过且 `started IS NULL` 转 `failed`：

```
排队 120 分钟没有 worker 领走（超过 30 分钟就算超时）；
这不代表它做完了，也不代表它失败了——是它根本没开始。
```
最后半句是必要的：只说「超时」和「卡住了」没有区别。

**③ 措辞**：
```
旧：…｜completed｜验证 pending｜第 1 次
新：…｜completed｜验证 pending｜第 1 次｜结果 incomplete（任务跑完了，但验收判据没达成）
```
改的是 `companion_agents.py` 的 `_summary()`——**只加字符串拼接，不碰状态机**。
摘要只在那个函数里生成，别处做不到。

---

## 5. 今天这类判断的共同点

今天有四次「指令或结论看起来合理，但查一下就站不住」：

| 场景 | 看起来 | 查完 |
|---|---|---|
| 队长要求 `SETTLED_OK` 加 `queued` | 排队不是失败 | 那个集合答的是「算不算进度」，加了就变成「做成了」 |
| 评审判 B-6 注释教旧字段名 | 注释过期 | 全文件该字段名出现 0 次 |
| 她说「task 带实参报 TypeError」 | 工具坏了 | 是修复前的旧状态 |
| 我判 `list` 参数无效 | 参数没作用 | 我没造出能让参数产生差异的输入 |

**四次都不是逻辑错，都是没先去看真实数据。**
最管用的动作始终是同一个：`grep`、`md5sum`、真实台账、真实 import、让她自己重试一次。

---

## 6. 已知未验证

- **`queued` 超时阈值与 capability-engineer 的对齐**：我用 1800 秒，他那边的阈值尚未合并（队长协调中）。两边都做兜底不冲突，**但阈值必须一致**，否则会出现「一边说还有效、一边说超时了」。
- **`companion_agents.py` 的 `_summary()`** 是我改的（合并冲突风险见提交说明）；如他重写该函数，以他的版本为基础重做这段文字即可。
- 超时清扫挂在 `_agent_pending_one` 上（有活儿就扫）。**如果长时间一件 agent 活都没有，清扫不会运行**——这在语义上可接受（没活儿就没有积压），但没有独立定时器验证过。
