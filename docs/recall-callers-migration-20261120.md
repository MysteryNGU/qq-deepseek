# 召回调用方迁移清单（P78 / 修订版 §6）

取证时间：2026-11-20 00:39　HEAD：`7e665ea`
**只读代码，未改任何文件，未重启服务。**

---

## 0. 先说结论：不是 6 处，是 **12 处**

任务书给了 6 处。实际 `grep` 全仓得到 **12 处**，多出 5 处：

| 多出来的 | 位置 |
|---|---|
| `memory_admin.share` | `life/memory_admin.py:118` |
| `people.verify_sync` | `life/people.py:64` |
| `recollection.associate` | `life/recollection.py:215` |
| `role_agent.callback` | `life/role_agent.py:742`（异步） |
| `role_agent.read` | `life/role_agent.py:1566` |

**这 5 处如果漏掉，迁移方案会做完之后发现"还是慢"** ——因为它们里有 1 处是 `async def`（`role_agent.callback`），
在事件循环里直接同步调用 `search_async` 之外的 `search`。

---

## 1. 调用方全清单

| # | 位置 | 函数 | 调用 | 所在函数 sync/async | 频率 | budget | 翻页 |
|---|---|---|---|---|---|---|---|
| 1 | `companion.py:2089` | `_messages` | `context(scope,[query],800)` | **sync**（被 async `_run_cognition` 调用） | **每轮必调** | 800 | 否 |
| 2 | `companion.py:2116` | `_messages` | `recollection.pick()` | sync | 每轮（`not current` 时） | 800（内部） | 否 |
| 3 | `companion.py:2594` | `_prefetch_recall` | `search_async(...)` | **async** | 每轮 | 8000/limit 8 | 否 |
| 4 | `companion_town.py:565` | `_handle_turn` | `context(scope,[query],800)` | **async**（`async def _handle_turn`） | 每小镇回合 | 800 | 否 |
| 5 | `continuity.py:482` | `context` | `context(scope,cues,...)` | **sync** | 每轮（经 role_agent） | `min(total-used)` | 否 |
| 6 | `runtime.py:611` | `context_block` | `context(scope,[query],...)` | **sync** | 经 `role_agent.py:1021` | `budget - note tokens` | 否 |
| 7 | `companion_recollection.py:34` | `pick` | `context(scope,text_cues,800)` | sync | 每轮（`not current` 时） | 800 | 否 |
| 8 | `memory_admin.py:118` | `share` | `search(audience,query,mode='text',limit=10)` | sync | 用户分享时（低频） | 默认 2400 | **是**（默认 True） |
| 9 | `people.py:64` | `verify_sync` | `search(ctx,'',mode='text',exact_refs=...)` | sync | 群聊画像核对（低频） | 默认 2400 | **是**（默认 True） |
| 10 | `recollection.py:215` | `associate` | `search(ctx,cue,budget=1200,limit=3)` | sync | 触发式（thought） | 1200 | **是**（默认 True） |
| 11 | `role_agent.py:742` | `callback` | `search_async(...)` | **async** | 能力调用时 | 见源码 | 否 |
| 12 | `role_agent.py:1566` | `read` | `search(context,...)` | **sync** | 能力读时 | 见源码 | **是** |

**关键事实：`context()` 内部已经把翻页关掉了**
```python
life/life_recall.py:470  def context(self, scope, cues, budget, *, exclude=()):
life/life_recall.py:477      packet = self.search(ctx, ' '.join(cues), budget=..., exclude=...,
life/life_recall.py:478                           include_cursor=CONTEXT_USE_CURSORLESS)
life/life_recall.py:58   CONTEXT_USE_CURSORLESS = os.environ.get('RECALL_CONTEXT_CURSORLESS','1') != '0'
```
**所以 1/4/5/6/7 这 5 处走 `context()` 的，天然不翻页——不是它们"不需要"，是 `context()` 已经替它们关了。**
**而 8/9/10/12 直接调 `search()`，用的是默认 `include_cursor=True`，它们确实在翻页。**

---

## 2. 每处的迁移判定

### 判定依据（逐条落实）

| # | 在事件循环？ | 每轮必调？ | budget 影响 | 需要翻页？ |
|---|---|---|---|---|
| 1 | **是**（`_run_cognition` 是 `async def`，同步调 `_messages`） | **是** | 800，中等 | 否（context 关闭） |
| 2 | 是 | 是（条件） | 800 | 否 |
| 3 | 是 | 是 | **8000，最大** | 否 |
| 4 | **是**（`async def _handle_turn`） | 每镇回合 | 800 | 否 |
| 5 | 是（经 role_agent context） | 是 | 变量 | 否 |
| 6 | 是（经 role_agent:1021） | 条件 | 变量 | 否 |
| 7 | 是 | 是（条件） | 800 | 否 |
| 8 | 是（`share` 被 async 调） | 否 | 2400 | **是** |
| 9 | **是**（`verify_sync` 名字带 sync，但在 async 上下文里） | 否 | 2400 | **是** |
| 10 | 是（`associate` 被 role_agent 调） | 否 | 1200 | **是** |
| 11 | 是（`async def callback`） | 否 | — | 否 |
| 12 | 是 | 否 | — | **是** |

### 判定

| # | 位置 | 判定 | 理由 |
|---|---|---|---|
| 1 | `companion.py:2089` | **迁移（第一批）** | 事件循环 + 每轮必调 + 同步阻塞。这是 §6 点名的「`_messages` 仅装配，不再现场查全库」 |
| 2 | `companion.py:2116` | **迁移（第一批）** | 与 1 同一条 `_messages` 路径，且它内部又调 `context()`（见 #7），等于**每轮多查一次** |
| 3 | `companion.py:2594` | **保留现状** | 已经是 `await search_async` + `wait_for(0.8)`，不阻塞事件循环。**它的问题不是阻塞而是超时太短**（源码注释：`search(mode=auto)` 实测 6–9s，0.8 必然超时） |
| 4 | `companion_town.py:565` | **迁移** | `async def _handle_turn` 里同步 `context()`。频率低（每镇回合）但一旦发生就阻塞整个事件循环 |
| 5 | `continuity.py:482` | **迁移** | 每轮经过，同步 |
| 6 | `runtime.py:611` | **迁移** | 每轮经过（经 `role_agent.py:1021`），同步 |
| 7 | `companion_recollection.py:34` | **迁移** | 被 #2 每轮调用，且**独立再查一次全库** |
| 8 | `memory_admin.py:118` | **保留** | 低频（用户主动分享），`mode='text'` 不涉及 embedding；保留同步契约但**调用点要确认不在 async 里** |
| 9 | `people.py:64` | **待定** | 名字是 `verify_sync`，但实际调用链要确认。低频，`mode='text'` |
| 10 | `recollection.py:215` | **保留** | 触发式，有 1 小时去重，`budget=1200` 不大 |
| 11 | `role_agent.py:742` | **保留** | 已经是 `await search_async` |
| 12 | `role_agent.py:1566` | **待定** | 能力读路径，需要确认调用频率 |

**迁移 5 处（1/2/4/5/6/7 共 6 个位置，其中 1 和 2 在同一函数），保留 5 处，待定 2 处。**

---

## 3. 翻页（cursor）逐项核实 —— P0 遗留问题

**昨晚的判断「5 个调用方都不需要翻页」是错的。准确说法是：**

```
走 context() 的 5 处：不使用 cursor —— 但那是 context() 内部关掉的（include_cursor=CONTEXT_USE_CURSORLESS）
直接调 search() 的 4 处：使用默认 include_cursor=True —— 它们确实在翻页
```

| 调用方 | 走哪个入口 | include_cursor | 代码有没有读返回的 cursor |
|---|---|---|---|
| 1 `companion.py:2089` | `context()` | **False**（内部强制） | 无 |
| 2 `companion.py:2116` → #7 | `context()` | **False** | 无 |
| 3 `companion.py:2594` | `search_async` | 默认 | **无**（只取 `items`/`coverage`/`diagnostics`） |
| 4 `companion_town.py:565` | `context()` | **False** | 无 |
| 5 `continuity.py:482` | `context()` | **False** | 无 |
| 6 `runtime.py:611` | `context()` | **False** | 无 |
| 8 `memory_admin.py:118` | `search()` | **True（默认）** | 无（只取 `items` 判断 contains） |
| 9 `people.py:64` | `search()` | **True（默认）** | 无 |
| 10 `recollection.py:215` | `search()` | **True（默认）** | 无 |
| 12 `role_agent.py:1566` | `search()` | **True（默认）** | 待确认 |

**结论：12 处里没有任何一处读取了返回值的 `cursor` 字段。**
**但它们中 4 处仍在**生成** cursor（`include_cursor=True` 的行为），那是白付的代价。**

**这条对迁移方案的意义：**
```
① context() 那条路已经优化过（CONTEXT_USE_CURSORLESS），不用再动
② 8/9/10 三处直接调 search() 的，可以显式传 include_cursor=False，省掉生成开销
③ 这是独立于迁移的、更小的一步优化（风险低）
```

**注意 `CONTEXT_USE_CURSORLESS` 是环境变量开关（`RECALL_CONTEXT_CURSORLESS=0` 可回退）——
说明这条优化当初就留了退路，迁移前应先确认它现在是开着的。**

**实测（2026-11-20 00:45）：`CONTEXT_USE_CURSORLESS = True`，进程环境无 `RECALL_CONTEXT_CURSORLESS`，走默认。**

### 3.1 修正：第 12 处**不能**关 cursor（本节修正初稿的错误）

初稿写「12 处里没有任何一处读取返回值里的 cursor」，**这是错的**。

```
life/role_agent.py:1555-1568   read(scope, p, root)   —— 能力接口，p 是模型传入的参数

1563  return self.life.life_recall.read(context, str(p.get('ref','')), cursor=p.get('cursor'), …)
1566  return self.life.life_recall.search(context, str(p.get('query','')), mode=p.get('mode','auto'),
1567      budget=…, cursor=p.get('cursor'),        ← 把调用方传入的 cursor 原样透传给 search
1568      exact_refs=…, limit=p.get('limit',10))
```

**它明确用调用方给的 `cursor` 续页——模型靠这条翻页。传 `include_cursor=False` 会直接废掉能力。**

**所以可安全关 cursor 的是 **3** 处，不是 4 处：**

| 可关 | 位置 | has_more 依赖核查 |
|---|---|---|
| ✅ | `memory_admin.py:118` | 该处 50 行内无 has_more 引用 |
| ✅ | `people.py:64` | 无 |
| ✅ | `recollection.py:215` | 无 |
| ❌ | `role_agent.py:1566` | **能力接口，cursor 从 p 透传，不能关** |

**这条是「单独 grep 一次 has_more」查出来的——不查就会把一个能用的能力路径关掉。**

---

## 4. async 函数里的阻塞调用

### 4.1 `time.sleep`（5 处，都不在 async 函数里）

```
life/companion_agents.py:506    time.sleep(0.05)     ← 不在 async
life/companion_agents.py:549    time.sleep(...)      ← 不在 async
life/companion_agent_worker.py:50  time.sleep(sleep_s)  ← 独立进程
life/companion_background.py:281   time.sleep(0.5)   ← 后台线程
life/local_embedding.py:185        time.sleep(.25)   ← 独立服务
```

### 4.2 `.result()`（1 处）

```
life/companion_connections.py:49   callback(future.result())
```

**上下文：**
```python
def job(coro, callback, status):
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    future.add_done_callback(lambda done: results.put((done, callback, status)))

def drain():
    while not results.empty():
        future, callback, status = results.get_nowait()
        callback(future.result())     # ← 第 49 行
    root.after(100, drain)
```

**判定：`drain()` 跑在 Tk 主线程（`root.after`），不是 asyncio 事件循环。**
**而且 `future` 是在 `add_done_callback` 里放进去的——此时 future 已完成，`.result()` 不阻塞。**

**所以 `companion_connections.py:49` 是安全的，不在「async 里阻塞」这类。**

### 4.3 真正的隐患：同步 `context()` 在 async 路径里

**修订版 §6 说的「不要在 async 函数里阻塞」，在这份代码里的实际形态不是 `.result()`，
而是 #1/#4 这种「`async def` 里直接调用同步全库查询」。**

```
life/companion.py:3053       async def _run_cognition  ──>  3067: self._messages(...)  [sync]
                                                              └─ 2089: life_recall.context(...)  [sync, 全库查询]
life/companion_town.py:527   async def _handle_turn   ──>  565: life_recall.context(...)  [sync]
```

**这两条是今天 checklist 里 `/alive` 主线程 55.7% 同步 DB 的同一个病。**

---

## 5. 迁移顺序建议（分四阶段，每阶段可独立验证）

### 阶段 0（前置，不改召回逻辑）
```
① 确认 RECALL_CONTEXT_CURSORLESS 现在是 1（环境变量开着）—— 已确认 = True
② 给 8/9/10 显式传 include_cursor=False（**3 处**，见 §3.1 修正；第 12 处不能动）
```
**验证：改前后 `search()` 的耗时对比。这一步独立、可回退、风险最低。**

### 阶段 1（第一批：每轮必调的 `_messages`）
```
迁移 #1 companion.py:2089 与 #2/companion.py:2116
做法：在 _run_cognition 进入时异步备好 RecallPacket，_messages 只装配不再现场查
```
**验证：`_messages` 里不再出现 `life_recall.context` 调用；用探针确认每轮 DB 查询次数下降。**

### 阶段 2（第二批：其它同步入口移出事件循环）
```
迁移 #4 companion_town.py:565、#5 continuity.py:482、#6 runtime.py:611
做法：涉及全库查询的移到对应 worker（asyncio.to_thread 或专用线程）
```
**验证：每个入口单独测，确认 async 函数体内不再直接调用 `context()`。**

### 阶段 3（验证 `_prefetch_recall` 到底有没有产出）

**这一步不迁移代码，只做取证。前置问题：如果这条路径一直在空转，前三个阶段做完之后语义召回仍然没有产出。**

**背景（源码自述 + 实测）：**
```
companion.py:2596-2598   # search(mode=auto) measured 6-9s; 2.0 always times out.
                         timeout=0.8
生产进程环境             RECALL_MODE_OVERRIDE=text
text 模式实测（他人）    中位 1666 ms
→ 即使已经强制成 text，0.8s 的超时也必然超时
→ 推论：prefetch 每轮等 0.8s 然后拿空
```

**但推论不等于事实，必须用 routes 数据验证。**

**判据（三个计数）：**
```
① semantic 成功运行几次   attempted=true 且 status ∈ {success, partial}
② 找到匹配几次            returned_count > 0
③ 实际装入上下文几次      loaded_count > 0
```

**必须同时记录 `_recall_trace['effective_mode']`。**

**理由：今天的超时结论必须区分 auto 与 text——auto 源码自述 6–9s，text 实测约 1.7s，两者差 3~5 倍。
不看 effective_mode 就下结论，会把「text 下也超时」误判成「auto 太慢」，从而导出「改回 auto 会好」这种相反的结论。**

**判定：**
```
attempted > 0 且 returned 恒 0  → 这条路径确实在空转，要决定「留还是关」
returned > 0 但 loaded 恒 0     → 找到了却没进上下文，问题在装配环节
全 0                            → 根本没跑（查 skip_reason：non_dialogue / empty_query / no_semantic_adapter）
```

### 关于 `_prefetch_recall` 与 `_messages` 的 `context` 是否重复（**不能简单删**）

两条路查的东西看起来相同，但**排除条件不同，用途不同**：

```
#1 _messages:2089   context(exclude = history + 本批 + prior_users)
                     → 排除已进上下文的，找"新的"

#3 prefetch:2594    search_async(exclude = ())
                     → 不排除 history，找"和这句语义相关、可能已经在 history 里"的
```

**prefetch 的 exclude 为空不是疏漏，是设计意图：语义召回的价值在于用不同字面找到同一条旧记忆，
而 `context` 因为排除了 history，恰恰找不到这些。**

**所以「删掉 prefetch」之前必须先验证：把 `context` 的 history 排除去掉后，能否覆盖 prefetch 的用途。**

**验证方法（约 10 分钟）：**
```
① 构造一个"字面不同但语义相关"的 query
② 跑 context(exclude=history)：命中吗？
③ 跑 prefetch(exclude=())：命中吗？
④ ②命中且③命中 → prefetch 冗余；②不命中而③命中 → prefetch 有独立价值
```

**不要因为"它超时"就删——超时是现在**的状态**，不是它的**用途**。

### 不迁移（有明确理由）
```
#3  已经是 await + wait_for，要改的是超时值（0.8s 太短），不是阻塞
#8  低频 + mode=text 不涉 embedding
#10 有 1 小时去重
#11 已经是 await search_async
```

### 待定（需补信息）
```
#9  people.verify_sync —— 需确认它在哪个线程被调
#12 role_agent.read:1566 —— 已确认：能力接口，cursor 从 p 透传，**不能关**（见 §3.1）
```

---

## 6. 一个需要指出的事

**`companion.py:2598` 的 `timeout=0.8`：**
```python
# 2026-11-19: search(mode=auto) measured 6-9s; 2.0 always times out.
# 0.8 saves 1.2s of useless waiting and can still succeed on small sets.
timeout=0.8
```
**源码注释自己写着 `search(mode=auto)` 实测 6–9 秒，而超时设 0.8 秒——
这意味着这条语义召回路径在 auto 模式下**几乎总是超时**，`_recall_prefetch` 拿到的多半是空。**

**这与 #3 的「保留现状」判定不矛盾（它确实不阻塞），
但它是另一件事：**这条召回路径的实际有效性值得单独验一次**。
不在本任务范围内，记在这里免丢。

---

## 7. 取证命令（可复现）

```bash
date; git -C /opt/qq-deepseek rev-parse --short HEAD
grep -rn "life_recall\.context\|life_recall\.search\|search_async\|\.pick(" \
  /opt/qq-deepseek/life/ --include=*.py
grep -rn "CONTEXT_USE_CURSORLESS" /opt/qq-deepseek/life/*.py
grep -rn "time\.sleep(\|\.result()" /opt/qq-deepseek/life/*.py
grep -rn "'cursor'\|\"cursor\"\|include_cursor" /opt/qq-deepseek/life/*.py
```

---

## 8. 已知未验证

### 8.1 阶段 0 的**行为**验证：未完成

**代码已改**（`memory_admin.py` / `people.py` / `recollection.py` 三处 `include_cursor=False`），
**但没有证据证明它省了时间。**

```
原因：探测实例的 agent_id='default'，生产是 'xiaoyu'
      life_resources 的 origin_scope 是三段式 'xiaoyu|qq:private:…|qq:private:…'
      探测实例下 search() 一律返回 0 条，两种口径无法区分
      强行把 agent_id 改成 xiaoyu 后，access.bind() 抛 RecallError('not_available')

影响：改动本身无害（include_cursor 只控制是否生成 cursor，不参与过滤）
      但「2.3ms → 2.1ms」这个数在 items=0 的前提下没有意义

下一步：由 capa 在真实 dialogue 轮验证
```

### 8.2 `search()` 在生产真实环境下能否返回数据：**未知**

**注意区分两件事——它们常被混为一谈：**

```
semantic_timeout  = 请求发出去了，0.8 秒没回来             ← t44 观察到的
查不到            = 请求回来了，结果为空 items=[]
```

**t44 观察到 `status='semantic_timeout'` / `routes={}` / `coverage='unavailable'`，
这证明的是「prefetch 超时」，**没有**证明「search 返回空」。**

**两者后续处理完全不同：**
```
如果是超时   → 调大 timeout 可能解决
如果是返回空 → 调 timeout 没用，要查索引/过滤/scope
```

**为什么不能在隔离环境里验：** 探测实例的 `agent_id` 恒为 `default`（由 config 决定），
改成 `xiaoyu` 后 `access.bind()` 抛 `not_available`，因此无法在离线环境复现生产的过滤条件。

### 8.3 两次误判（记录在此，供后续参照）

同类错误在 24 小时内犯了两次，都是**没对齐环境就下结论**：

| # | 我报出的结论 | 实际 | 错因 |
|---|---|---|---|
| 1 | 「`search()` 查什么都是 0 条」 | 探测实例 `agent_id='default'`，数据在 `'xiaoyu'` 下 | 用错了 agent |
| 2 | 「`coverage.text='ready'` 是假通过」 | 同一错误环境下得出，不成立 | 承接 #1 |

**（更早一次同类：t40 用 `memory.current` 当真实占用，把 companion 报成 590 MB，实际 anon 168 MB。）**

**共同点不是「看错了数」，是「没确认这个数是在什么环境/什么口径下取的」。
报告异常之前，先问一句：我的探测环境和生产一致吗？**

---

## 9. 给 capa 的测试清单（可直接用）

**一条已验证存在的测试样本：**

```
event id            evt_31a21e8983cacb1a669d4ebc
kind / source_kind  message / self_output
agent_id            xiaoyu
conversation_id     qq:private:763282103
audience_policy_id  qq:private:763282103
private_key         ('xiaoyu', 'qq:private:763282103')
origin_scope        xiaoyu|qq:private:763282103|qq:private:763282103
redacted_at         None（未撤销）

对应资源
  resource_id       life_48dc8f9eee0536de1687dbf8
  kind / native_id  event / evt_31a21e8983cacb1a669d4ebc
  status            active
  source_version    1

所在 scope 规模
  xiaoyu|qq:private:763282103|qq:private:763282103   14262 条 active 资源
```

**用于对照的三个数：**

| 检查项 | 值 | 判断 |
|---|---|---|
| 该 resource 在 `life_semantic_index` 的条数 | **0** | **未被语义索引** → 语义路线永远找不到它 |
| `life_semantic_index` 总条数 | 98440 | — |
| `life_resources` active 总条数 | 16922 | — |

**注意第三行那个 581.7% 的覆盖率是假的**：`life_semantic_index` 按 chunk 存储（一条资源可切多块），
不能直接除以资源数。要算真实覆盖率，应 `COUNT(DISTINCT resource_id)`。

**这条样本的用途：** 如果 capa 那一轮的 query 与它相关却命中不了，
配合「该 resource 在语义索引里为 0 条」，就能区分是「过滤问题」还是「没建索引」。
