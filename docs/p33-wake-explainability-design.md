# P33 唤醒可解释性：设计（不碰 companion.py 的部分）

日期：2026-11-18。状态：**设计与测试设计，未改 `companion.py`**。
基线：**云端 `life/companion.py` md5 `c32c180e1bd7463cd522b8cbc966433e`，3343 行**
（2026-11-18 于 captain 的 queued 修复后重取）。

> **基线换过两次，这就是为什么行号不能凭记忆。**
> - `837513ab` 3339 行：原始基线
> - `c32c180e` 3343 行：captain 修了 `_agent_pending_one` / `_worker` 两处 SQL
>   （`status='pending'` → `status IN ('pending','queued')`），净增 4 行注释
>
> 两次之间实际偏移只有 4 行（`_agent_worker_one` 3075 → 3079），其余位置不变。
> 但下一次可能就不止 4 行——**每次动手前先 `md5sum` 现取**。

> 本文所有 `life/companion.py:NNNN` 均指上面那一份。

---

## 0. 一句话设计

**不新建调度器，只扩展现成的 `_record_scheduler_probe`**：
把「为什么这一拍没触发」从**单源**扩成**四源盘点 + 每个源等了多少 + 超阈值可见**，
并在真正唤醒时往 `companion_activity` 追加一条 `wake`。

---

## 1. 现状：可解释性已有一半

云端 `life/companion.py:866` 的 `_record_scheduler_probe()` 每 5 秒跑一次（`:2871-2872`），
已经在回答「**为什么这一拍没有触发**」：

```python
probe['blocked'] = 'cognition_locked' | 'execution_running' | 'autonomy_off'
                 | 'candidate_ready' | 'waiting_next_wake' | 'no_candidate'
```

`:3309` 已挂到 `observability['scheduler']`，运行详情可见。

**它缺的三件事**（就是 P33 要补的）：

| 缺什么 | 后果 |
|---|---|
| 只看 `followthrough.due()` **一个源** | 子 agent 台账、B站待办、用户消息完全不在视野里 |
| **不记任何时长** | 无法回答「有没有事件一直等不到」 |
| **不记实际唤醒** | 只能看「没醒」，看不到「醒了，因为什么，等了多久」 |

---

## 2. 五个源，各自「等了多久」怎么算

每个源都有现成的时间戳，**不需要新表、不需要新字段**。

| 源 | 读哪里 | 「等了多久」= now − | 说明 |
|---|---|---|---|
| **用户消息** | `self._input_batch[0]['at']`（云端 `:1775` 写入） | 最早一条入批时间 | 合并窗口内的消息，等的是批处理 |
| **项目到期** | `followthrough.due()` 返回项的 `due_at`（`life/companion_followthrough.py:232`） | `due_at` | 已到期多久没被认领 |
| **B站来信** | `companion_bili_inbox` 的 `occurred` / `created` / `next_due` | `next_due` | 到了 due 还没处理 |
| **子 agent：台账** | `companion_agent_tasks` 的 `updated`（`life/companion_agents.py:640`） | `updated` | **最后状态变化**，不是创建时间 |
| **子 agent：未认领行动** | `companion_actions` 里 `kind='agent'` 且 `status IN ('pending','queued')` 的 `updated` | `updated` | **§8.2 实测：此刻就有 3 条卡在这里，最久 2.6 小时** |

**最后一行是实测后补的**，原设计漏了它。区别很关键：

- `companion_agent_tasks` 是**已受理**的子 agent 台账（任务进来了）；
- `companion_actions` 里 `kind=agent` 的行是**还没被 worker 领走**的行动。

今晚 t15 那个 bug（`_worker()` 的 SQL 排除 agent、`_agent_worker_one()` 只读台账，
中间没有转移）卡住的正是**后者**。只盯台账会漏掉今天这种形态。

**子 agent 那条用 `updated` 而不是 `created` 是有意的**：卡死检测关心的是
「状态多久没动了」，不是「这件活开了多久」。一件跑了 20 分钟但一直在推进的任务不该告警。

### 2.1 项目到期有个坑，必须处理

`followthrough.due()`（`life/companion_followthrough.py:197-242`）**只返回 `min()` 一个**
（`:242`）。也就是说：**排在后面、同样已到期的项目，probe 根本看不见**——
而这正是「有事件一直等不到」的典型场景。

处理办法（两选一，我建议 A）：

- **A（推荐，零侵入）**：`due()` 加一个可选参数 `all_candidates=False`，
  为 True 时返回完整列表而不是 `min()`。**默认行为完全不变**，现有调用方一行都不用改。
- B：在 probe 里重新算一遍候选。**不推荐**——等于把 `due()` 的判定逻辑抄一遍，
  两份实现迟早不一致。

---

## 3. 饥饿阈值

今晚的真实事故是**子 agent 停在 pending 13 分钟无人管**。按这个量级定：

| 源 | 阈值 | 依据 |
|---|---|---|
| 用户消息 | **30 秒** | captain 给的建议；`_input_window` 默认 2.5 秒，30 秒是它的 12 倍，正常合并不会误报 |
| 项目到期 | **5 分钟** | captain 给的建议；`due_at` 已过 5 分钟还没认领，说明被别的东西一直占着 |
| 子 agent（未认领行动） | **60 秒** | 一条 `kind=agent` 的 pending/queued 行动 60 秒还没被领走，说明 worker 循环有问题——正是 t15 的形态。**§8.2 实测现在有 3 条违反了它** |
| 子 agent（台账无变化） | **60 秒** | 卡死检测。低于 60 秒会误报——一次正常工具调用就可能超过它 |
| B站来信 | **10 分钟** | 社交消息不如项目紧急；且 `settle(..., after_seconds=5)` 自带重试，10 分钟留够重试窗口 |

**阈值必须是命名常量，写在一个地方**，不要散落在判断里：

```python
STARVATION_SECONDS = dict(user_message=30.0, project_due=300.0,
                          agent_action=60.0, agent_task=60.0, bilibili=600.0)
```

---

## 4. 写入策略（captain 的三条要求）

### 4.1 不在每秒循环写库 —— 实测确认

| 什么时候 | 写什么 | 频率 |
|---|---|---|
| 每 5 秒（沿用现有 probe 节奏） | `_state('last_starvation')` —— meta 表一行，覆盖写 | 每 5 秒 1 次（**与现在完全相同，不增加**） |
| **真正唤醒时** | `companion_activity` 追加一条 `wake` | 每次唤醒 1 条 |

```python
_hunger = time.time() - self._state('last_wake_log_at', 0)
if _hunger < 1.0:      # 每拍只写一条，其余全是 no-op
    return
```

**关键：`_scheduler` 里那条 `sleep(1)` 循环本身不写库**，只在满足条件时把一致性标记清掉。
`_record_scheduler_probe` 已经受 `>=5` 秒门控（`:2871`），**一行都不改它的节奏**。

### 4.2 追加不覆盖

`_audit`（`:615`）已经是 `INSERT INTO companion_activity`，天然追加。
`wake` 记录走同一条路径，所以「看得出趋势」这条自动满足——
并按 `created` 排序就能看出「哪类事件反复饥饿」。

### 4.3 降采样

同类事件 1 分钟内只记一次。实现放在 `_wake_log` 里，用 `_state` 记上次记录时间：

```python
key = 'wake_log_at:' + trigger
if now - self._state(key, 0) < 60: return   # 1 分钟内同类只记一次
```

**降采样只影响 activity 记录，不影响 probe 的饥饿标记**——饥饿要在 `probe` 里
每秒可见，不能因为降采样而漏报。

---

## 5. 记录格式

沿用 `companion_activity(type='wake')`，`details` 字段：

```python
dict(
  trigger   = 'user_message' | 'project_due' | 'bilibili' | 'agent_result' | 'agent_task' | 'timer' | 'touch',
  candidates= [dict(source=..., count=..., oldest_age_s=..., sample='前 40 字')][:4],
  chosen    = dict(source=..., ref=..., age_s=...),
  why       = '项目到期且 autonomy=true' | '子 agent 优先于普通行动' | ...,
  waited_s  = round(now - chosen_at, 1),
)
```

**`candidates` 只放同类竞争的源，不是全量**（按 captain 要求）。
`sample` 截 40 字，避免把正文写进审计。

---

## 6. 要在 `companion.py` 加的四个函数（等 t13 落地后写）

1. `_starvation_snapshot(now)` —— 四源盘点，返回每个源的 `count` / `oldest_age_s` / `starved`。
   **纯读，不写库**，供 probe 每 5 秒调用。
2. `_wake_log(trigger, chosen, why, candidates)` —— 写 `companion_activity`，
   含降采样与 `_hunger < 1.0` 短路。
3. `_record_scheduler_probe()` —— **扩展现有函数**：保留全部原字段与 `blocked` 语义，
   只**增加** `starvation` 与 `starvation_alert` 两个键。
4. 快照 `:3309` 附近 —— 把饥饿告警提到 `observability` 顶层，运行详情直接可见。

**不改**：`_scheduler` 的分支顺序、`_workers` 的三路顺序、任何 `if` 条件。

---

## 7. 测试设计（临时 home + 假时钟，不碰生产）

测试文件（**计划中，尚未创建**）：`tests/test_wake_explainability.py`，全部用 `tempfile.TemporaryDirectory()`
+ 注入固定 `now`，不连生产库、不发网络。

| 用例 | 构造 | 断言 |
|---|---|---|
| T1 用户消息 + 项目到期同时就绪 | 两者都就绪，项目 `due_at` 比消息早 | `wake` 记录里 `chosen.source='project_due'`，`why` 写明排序依据 |
| T2 子 agent 结果 + 普通行动同时就绪 | `_agent_worker_one` 有活、`_worker` 有活 | 记录 `chosen.source='agent_task'`，`why` 含「子 agent 优先」 |
| T3 饥饿告警 | 子 agent `updated` 是 13 分钟前 | `probe['starvation_alert']` 里出现该源，`age_s` ≈ 780 |
| T4 不误报 | 子 agent `updated` 是 30 秒前 | **没有** `starvation_alert` |
| T5 每拍只写一条 | 连续调 `_wake_log` 10 次（同一秒） | `companion_activity` 只多 1 行 |
| T6 降采样 | 同类事件间隔 10 秒各一次 | 只记 1 次；间隔 61 秒 | 记 2 次 |
| T7 probe 不写 activity | 只跑 `_record_scheduler_probe` 10 次 | `companion_activity` **零新增**（关键：证明每秒循环不写库） |
| T8 调度顺序不变 | 四源全部就绪 | 实际唤醒顺序与改动前一致（对照基线快照） |

**T7 是对 captain「不能每秒写库」的硬验证**，T8 是对「不改变调度顺序」的硬验证。

---

## 8. 生产数据校准（实测）

用云端 `life.db`（只读）核对阈值是否合理。**结论：阈值合理，而且现场就有饥饿实例。**

### 8.1 子 agent 台账（`companion_agent_tasks`）

```
status 分布: completed 1 / interrupted 1      ← 样本只有 2 条
created→updated 间隔: n=2  min=41s  p50=54s  max=54s
超过 60s 的: 0 / 2
```

**样本太少（n=2），不足以判断 60 秒阈值会不会误报一次正常的长期任务。**
但至少说明：现有两条都在 54 秒内结束，60 秒阈值不会在这两条上误报。

### 8.2 但真正的饥饿在另一张表：`companion_actions` 里 `kind=agent` 的动作

```
status 分布: completed 1005 / failed 166 / cancelled 11 / interrupted 3 / queued 3

kind=agent 的动作，按创建时间倒序：
  cancelled  updated_age =  2129s
  queued     updated_age =  2150s   ← 停在 queued 35 分钟
  queued     updated_age =  2169s
  failed     ...
  queued     updated_age =  9389s   ← 停在 queued 2 小时 36 分
```

**现在就有 3 条 `kind=agent` 动作停在 `queued`，最久 9389 秒（约 2.6 小时）。**

这正是今晚「子 agent 停在 pending 13 分钟没人管」的同类问题，而且**此刻仍在发生**。
它证实了 P33 的必要性：没有任何界面或审计能让人一眼看到「这三件活卡了两小时」。

### 8.3 由此对设计的两个修正

1. **饥饿检测必须同时看 `companion_actions`（kind=agent 且 queued/pending）**，
   不能只看 `companion_agent_tasks`。后者是台账（已受理的任务），前者是**尚未被领走的行动**——
   今晚卡住的恰恰是后者。原设计 §2 的「子 agent」两行要拆成两个源：
   `agent_task`（台账 running 无变化）与 `agent_action`（actions 里 kind=agent 且未认领）。

2. **60 秒阈值对「已受理」合适，对「未认领」要更短**：
   一条 `kind=agent` 的 queued 动作 60 秒还没被 `_agent_pending_one` 领走，
   说明 worker 循环有问题——这正是 t15 那个 bug 的形态。建议
   **`agent_action` 阈值 60 秒**，与 `agent_task` 分开计。

### 8.4 仍未验证

1. **60 秒对长任务是否误报**：台账样本只有 2 条（都 < 54s），无法判断。
   需要积累更多数据，或先观察一周再收紧。
2. `followthrough` 的 `due_at` 在生产上多久出现一次「多个同时到期」——没查到数据。

---

## 9. `followthrough.due()` 加 `all_candidates`（**已批准，在 t14 范围内**）

captain 已批准，范围包括 `life/companion_followthrough.py`。

**硬要求**：默认 `False` 必须**完全保持现有行为**——返回单个 `dict` 或 `None`，不是列表。

```python
def due(self, *, now=None, autonomy=True, delegated_refs=(), all_candidates=False):
    ...
    ordered = sorted(candidates, key=lambda p: (p['due_at'], p['ref']))
    return ordered if all_candidates else (ordered[0] if ordered else None)
```

**必配测试**（captain 要求）：

| 用例 | 断言 |
|---|---|
| 不传参数 | 与改动前**完全一致**：单个 dict 或 None，内容相等 |
| `all_candidates=True`，单候选 | 返回长度 1 的列表，元素与不传时相等 |
| `all_candidates=True`，多候选 | 按 `(due_at, ref)` 排序，且**首个元素 == 不传参数时的返回值** |
| 无候选 | 不传 → `None`；传 `True` → `[]` |

第三条是关键：它把「两种模式看到的是同一批候选」钉死，防止以后两边算法漂移。

---

## 10. 阈值先只告警不阻断（captain 要求）

`agent_task` / `agent_action` 的 60 秒阈值**证据不足**：台账样本只有 2 条（都 < 54s），
无法判断长任务会不会误报。

**因此**：饥饿检测**只写 `companion_activity` 与 `observability`，不阻断任何流程、
不取消任何动作、不改变调度**。观察一周真实分布后再决定是否收紧。

落实方式：`_starvation_snapshot` 是**纯读**函数，返回值只被记录与展示，
**没有任何调用点会因它而为真时改变分支走向**。这一点要有测试守着
（`test_probe_does_not_write_activity_every_tick` 之外再加一条「告警不改变调度结果」）。

---

## 10.5 实现与部署记录（2026-11-18 09:09，已上线）

**云端 `life/companion.py` sha256 `9485953a3e5bb55c60765644b8275f83ac49a7a7df41b06ae1a6e1e1edcebf41`**
（基线 `b9f69dea` + 162 行纯新增，**0 行删除**）。

| 项 | 结果 |
|---|---|
| 新增位置 | 常量 `STARVATION_SECONDS` / `WAKE_LOG_DEDUPE_SECONDS`；方法 `_starvation_snapshot` / `_wake_log`；probe 加两个字段；observability 加 `starvation`；三个唤醒点各加一次 `_wake_log` |
| 是否只加不改 | **是**：对基线 diff 为 +162 / −0 |
| 真实 `_messages()` | 通过：dialogue ok / background ok / 4 messages（云端实跑） |
| probe 是否写库 | **不写**：上线后 `companion_activity` 中 `type='wake'` 行数 = 0 |
| `blocked` 结论是否变化 | **不变**：线上 probe 仍是 `waiting_next_wake`，只是多了 `starvation` 字段 |
| 饥饿能否触发 | **能**：注入 2151 秒的 `queue.agent_action` → `starved_count=1`，告警文案正确 |
| 服务 | active/running，`/alive` 200，重启后 `Traceback|CRITICAL` 计数 0 |

测试：`tests/test_wake_explainability.py`（17 项）+ `tests/test_wake_log_runtime.py`（12 项），
合计 29 项通过。运行时用例跑在真实 `LifeRuntime` 上，方法本身来自 `Companion`。

**一个部署发现的缺口**：`docs/deploy-checklist.md` §2.2 让跑
`.review-server/_t4_regression_live_round.py`，但该脚本 import `life.tests`，
而 **`life/tests/` 目录在云端不存在**（只在本地仓库有）。
本次改用等价的内联脚本完成同一判据（真 `Companion` + 真 sqlite + 桩模型 + 真调 `_messages`），
结果一致。**清单本身需要补一句**：要么把 `life/tests/` 一起部署，要么把脚本里那行
import 去掉。

---

## 11. 附：这次分叉涉及的另一处修复（与本任务无关，但同一文件）

排查 `life/companion.py` 本地/云端差异时，顺带核实了 t11 那条 `node=work` 硬伤。
**它的完成度是「部分完成」，写在这里免得有人以为已经修好。**

t11 的结论是「只改说明不够」，完整修复要 A + C 两步：

| 步骤 | 内容 | 状态 | 证据 |
|---|---|---|---|
| **A 说明写清** | 给模型看的 `dsh` 工具说明补上 node/work/echo 三种 node 的语义 | **已完成**，但**只在本地** | 本地 `life/companion.py` 有那 3 行，云端没有；本行属于本地独有的那部分分叉 |
| **C 前置拒绝** | `create()` 在 `node=work` 却没给 `command` 时直接拒掉，而不是派出去再失败 | **未完成**（已由 captain 派给 isolation-verifier） | 见下 |

### C 步的实测状态

`OP_FIELDS['create']` 两边不一样：

```
本地  life/companion_agents.py:  'create': {'goal', 'command', 'deliverables', ...}   ← 有 command
云端  life/companion_agents.py:74 'create': {'goal', 'deliverables', ...}              ← 没有 command
```

**所以这一项也是分叉的**：`command` 字段只在本地。合并时两边都要保留。

**结论：在 C 步落地前，`node=work` 缺 `command` 仍会「派出去再失败」，
而不是「派之前就拒绝」。** 说明文字能减少模型犯错，但挡不住犯错的。

（本节与 P33 的可解释性无关，只是同一文件同一轮排查的副产品。
记在这里是因为：不知道这件事的人，会以为 t11 已经修完了。）
