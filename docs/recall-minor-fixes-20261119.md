# 召回小修与观测（2026-11-19，设计 §8.1）

对应设计：`docs/superpowers/specs/2026-11-19-open-issues-resolution-design.md` 第 8 节第 1 步「小修与观测」。

提交：`cfde8f0`。改动文件：`life/life_recall.py`、`life/companion.py`。

---

## 零、P0 级功能缺陷：找记忆这条路从来没工作过（先说最要紧的）

**这一节写在最前面，因为它是"她好像记不住"这个用户感受的直接原因。**

三条独立的静默失败，每一条都是"代码没报错，但什么也没给你"：

| # | 缺陷 | 后果 | 证据 |
|---|---|---|---|
| 1 | `context(budget=800)` 必然抛 `budget_requires_page`，被静默吞成 `''` | 5 处 `context()` 里**3 处硬编码 800**，从来不可能返回内容 | 见下 |
| 2 | `search_vectors` 被 `scan_limit=10000` 截断，`return [], 'partial'` | 语义路线贡献恒为 0 | allowed 9874 资源 × 6.4 片/资源 = 62818 片 > 10000 |
| 3 | `mode=text` 时 `search_vectors` 根本不调用（`query_vector` 只在 auto/semantic 时算） | 生产当前状态下语义路线完全不参与 | 生产探针 5 分钟 0 条 `diag` 记录 |

### 0.1 空结果骨架就要 1154 token

```python
# life/life_recall.py:326-332
while estimate_tokens(result) > budget and items:
    items.pop()                                  # 逐条删
    ...
if estimate_tokens(result) > budget:
    raise RecallError('budget_requires_page')    # 删空了还超 → 抛错
```

**实测：**

```
budget=200   → budget_requires_page
budget=400   → budget_requires_page
budget=600   → budget_requires_page
budget=800   → budget_requires_page      ← 生产用的就是 800
budget=1000  → budget_requires_page
budget=1200  → OK  count=0  estimated_tokens=1162   骨架=1154
budget=1600  → OK  count=1
budget=2400  → OK  count=3
budget=4000  → OK  count=2               ← 注意：更大反而更少（limit 与 budget 共同作用）
```

**所以 `budget < 1162` 时，无论有没有命中，必然抛异常。**

骨架为什么这么大：`packet()` 里的 `next_cursor` 是一个签名的 base64 分页游标，
骨架 JSON 长度 2418 字符，绝大部分是它。**而 `context()` 只需要文本，根本不需要游标。**

### 0.2 三处硬编码 800 的调用点

```
life/companion.py:2089              context(self.scope, [query], 800, ...)        ← _messages
life/companion_town.py:565          context(self.scope, [query], 800, ...)
life/companion_recollection.py:34   context(self.scope, text_cues, budget)       ← pick 默认 800
life/continuity.py:482              context(scope, cues, max(0, total-used), ...)  ← 视情况
life/runtime.py:611                 context(scope, [query], max(0, budget - ...), ...) ← 视情况
```

**`_messages` 里那行是：**

```python
recall_candidates += [dict(item, age=_ago(item.get('occurred_at')))
    for item in compact_recall(self.life.life_recall.context(self.scope, [query], 800, ...))]
```

**`context()` 抛 `budget_requires_page` 后在 `life_recall.py:418` 被静默捕获返回 `''`，
`compact_recall('')` 返回 `[]`（实测），所以这一段的召回候选**恒为空**。

**不是"她记不住"，是"找记忆的那一步，预算不够，所以什么都没拿到，而且系统没有告诉她"。**

### 0.3 待修（另开任务，不在本次范围）

```
方向 A：search() 加 include_cursor=False，context 用；骨架降到几十 token
        前提：先列清 5 处调用方各自需不需要翻页
        （不需要翻页的继续用 context，需要的应该用 search）
方向 B：复核 5 处 budget 取值，确保高于当时的骨架大小
方向 C：scan_limit 截断改为 break 而非 return []（保留已扫部分）
```

**验证前必须先确认对照组的输出不是空的，否则比对无意义（见第 六 节）。**

---

## 一、三项修复

### ① 统一 `resolve_mode` 入口（设计 §2）

**问题。** `search_async` 在算 query vector 之前用 `options.get('mode', 'auto')` 判断模式，
而 `search()` 内部才读 `RECALL_MODE_OVERRIDE`。两处读的不是同一个值：

```
_prefetch_recall 显式传 mode='auto'
  → search_async 认为要走向量 → 算一次 embedding（实测 184ms/次）
  → search() 读到环境变量 text → 实际只走词法
  → 那 184ms 白花
```

**改法。** 新增模块级入口，两处共用：

```python
def resolve_mode(mode=None, *, ignore_override=False):
    override = os.environ.get('RECALL_MODE_OVERRIDE') if not ignore_override else ''
    if override:
        if override in _RECALL_MODES:
            if mode is not None and mode != override:
                log.warning('召回 mode 被生产 override 改写：请求=%s 生效=%s', mode, override)
            mode = override
        else:
            log.warning('忽略无法识别的 RECALL_MODE_OVERRIDE=%r', override)
    if mode is None:
        mode = 'auto'
    return mode
```

`search_async` 在算向量之前解析一次，把结果显式传给 `search()`；
`search()` 也走同一个函数，直接调用时行为一致。
测试要忽略 override 时传 `ignore_mode_override=True`，不走隐式绕过。

**优先级（实测 6 组，全部通过）：**

| 环境变量 | 显式 mode | ignore_override | 结果 |
|---|---|---|---|
| 未设 | None | 否 | `auto` |
| 未设 | `text` | 否 | `text` |
| `text` | None | 否 | `text` |
| `text` | `auto` | 否 | **`text`**（缺陷修复点） |
| `text` | `auto` | 是 | `auto` |
| `text` | None | 是 | `auto` |

**验证方式。** 计数器包住 `MemoryIndex.query_vector`，text 模式下调用次数为 **0**
（修复前传 `mode='auto'` 时必然调 1 次）。

### ② 每轮进入先清空召回状态（设计 §2）

**问题。** `_prefetch_recall` 的非 dialogue 分支在清空 `_recall_prefetch` **之前** `return`：

```python
async def _prefetch_recall(self, query, excluded, purpose=None):
    if purpose is not None and purpose != 'dialogue':
        self._recall_status = 'skipped_non_dialogue'
        return                        # ← 上面三行清空没执行
    self._recall_prefetch = []
    ...
```

`_messages` 随后读这些实例字段，于是上一轮 dialogue 写的候选会被带进这一轮 activity。

**改法。** 清空移到函数最前面，判空与跳过判断之后仍保持空值。

**验证方式。** 人为预置脏状态后跑 activity 轮：

```
预置: _recall_prefetch=[{id:'STALE', match_routes:['semantic']}], status='semantic_candidates'
跑完: 候选=[]  status='skipped_non_dialogue'  泄漏=False
```

设计原文标注「尚未以真实泄漏案例复现」——这里用人为预置脏状态复现了**代码路径**，
证明修复后不再残留。**真实发生过的泄漏样本仍未捕获。**

### ③ 每轮 recall 记录（设计 §5）

**目标。** 复用 `companion_context_loads` 的 JSON 字段，每轮认知记一个 recall 对象，
不新建每步骤流水表。不带原文查询、不带私聊正文。

**落地位置（重要）。** 第一版放在 `_last_material` 的**顶层新键**，实测**被丢弃**：

```python
# life/companion_evidence.py:record_load
slots = {slot: dict(row) for slot, row in report.items()
         if isinstance(row, dict) and 'used' in row}      # 必须带 used 键
```

而 `companion_life_material.assemble()` 对每个 slot 都会重建
`report[slot] = dict(used=..., cap=..., dropped=...)`，所以塞进 slot 的其他键也会被覆盖或过滤掉。

**最终方案：** 并入已有的 `recall` slot 条目，键名 `routes_detail`，并确保该条目有 `used`。
`_messages` 里这次合并发生在所有 `assemble()` 调用**之后**，不会被重装配冲掉。

**记录内容：**

```json
{"requested_mode": null, "effective_mode": "auto",
 "skip_reason": null, "routes": {}, "candidates": 0, "authorized": 0, "returned": 0,
 "query_embed_ms": null, "total_ms": 109.0,
 "status": "semantic_no_matches", "coverage": "partial", "semantic_items": 0,
 "index": {"model": "...", "chunker": "utf8-paragraph-v2-drop-blank:..."}}
```

**为什么需要配套改 `life_recall.py`。** 原 `diagnostics` 只有
`intent/returned/remaining/visibility/empty_reason/history`，没有 mode 与路线计数。
补了 `mode` / `candidates` / `authorized` / `route_counts` / `route_status`（只增不改）。

**验证方式。** 临时库上走真实顺序 `_prefetch_recall → _messages → record_load`：

```
activity: status=skipped_non_dialogue   coverage=unavailable
dialogue: status=semantic_no_matches    eff=auto  total_ms=109.0  coverage=partial
```

生产中已验证：部署后 25 分钟内出现 4 条带 `routes_detail` 的记录。

## 二、三个计数怎么查

设计 §5 要求分清三个数：**semantic 成功运行 / 找到匹配 / 实际装入上下文**，
并且 **text 跳过不算失败**、**分母按合资格 dialogue 请求**、**不能用"日志 0 条"证明成功**。

```sql
-- 取出时间窗内的记录
SELECT decision_id, at, slots
FROM companion_context_loads
WHERE agent_id='xiaoyu' AND at > :since
ORDER BY at DESC;
```

逐行解析 `slots -> recall -> routes_detail`，按 `status` / `skip_reason` / `semantic_items` 归类：

| 计数 | 判据 |
|---|---|
| 合资格 dialogue 分母 | `skip_reason != 'non_dialogue'` |
| text 跳过（不计失败） | `status = 'skipped_non_dialogue'` 或 `skip_reason = 'non_dialogue'` |
| semantic 超时 | `status = 'semantic_timeout'` |
| semantic 成功运行 | `status in ('semantic_candidates', 'semantic_no_matches', 'semantic_unavailable')` |
| 找到匹配 | `status = 'semantic_candidates'` |
| 实际装入上下文 | `semantic_items > 0` |

一条可直接跑的脚本在 `ops/scripts/recall_counts.py`（见附录）。

**注意：**"semantic 成功运行"不等于"召回结果被用上"。
`semantic_no_matches` 说明跑了但没命中；只有 `semantic_items > 0` 才代表进了请求体。
只数"没有超时"会高估。

## 三、那个 UnboundLocalError（同一类 bug 以后怎么防）

**怎么引入的。** 把 recall 记录从顶移位到 slot 里时，我删掉了原来 `_last_material_extra` 上方
那段 `trace` 构造，但没发现 `_last_material_extra` 里还有一处引用：

```python
self._last_material_extra=dict(..., semantic_candidates=trace['semantic_items'])  # trace 定义在下面
```

`ast.parse` 通过（语法合法），import 也通过（不执行函数体），只有真正调 `_messages` 才炸。

**怎么被发现的。** 部署脚本 `t76_deploy.sh` 用临时空库跑三个 purpose 的 `_messages`：

```
dialogue   ok=FALSE UnboundLocalError: cannot access local variable 'trace'
background ok=FALSE ...
work       ok=FALSE ...
DEPLOY_FAILED
```

**防法（已固化在部署脚本里）：**

```
1. ast.parse      —— 只能抓语法错
2. 真实 import    —— 只能抓导入期错
3. 用空库初始化 Companion，跑 _messages(dialogue/background/work)  ← 抓函数体内的错
4. 三项都过才打印 DEPLOY_OK，否则 DEPLOY_FAILED
```

第 3 步是关键：**函数体内的错误，前两步都抓不到。**

## 四、重启前后对比

```
                    基线 e76b345          改后 cfde8f0
/alive 中位         约 8ms               约 1.4ms
/alive 峰值（20 次） 1.56s                10.18s / 2.85s / 0.95s
CPU                 0.66 核              0.86 核
WARNING/ERROR       无                    无
索引                done 19824           done 21263，pending 9266
```

**峰值变差，未归因。** 样本只有 20 次，且当天 `/alive` 一直存在 5~20 秒的偶发峰值
（见 `docs/recall-mode-ab-20261119.md` 第六节未定论第 1 条）。
既不能归因于本次改动，也不能排除——**记为未定论**。

## 五、`/alive` 的机制终于定位：主线程几乎没有空闲

追 `/alive` 峰值追了一整天，最后用「主线程在干嘛」而不是「某次请求为什么慢」定位到了。

**测量**：`py-spy record --rate 100` 跑 45 秒，2523 个 MainThread 样本；同时每 5 秒打一次 `/alive`，
抓到两次慢请求（4.486s、8.034s）。

**样本分布：**

```
37.9%  Repository._all          ← 同步 DB
17.8%  Repository._one          ← 同步 DB
 7.7%  JSONDecoder.raw_decode
 4.2%  JSONEncoder.iterencode
 2.3%  EpollSelector.select     ← 真正空闲只有 2.3%
 2.1%  LifeCatalog._live
 2.1%  sha
 1.5%  LifeRecall.search
 1.5%  LifeRecall.allowed
 1.5%  Spending.snapshot
```

**同步 DB 合计 55.7%，主线程只有 2.3% 的时间在等 IO。**

机制：`Repository` 是**单连接 + 全局 `threading.RLock`**（`life/repo.py:42,45`），
所有查询串行；当有协程持续查询时，主线程大部分时间在跑 DB，
事件循环很少拿到控制权，`/alive`（与所有协程同一事件循环）排不上队。

**所以这不是 recall 专属问题，是整个进程的 DB 访问模型问题。**
设计 §3「专用读 session」与 §4「SQLite 与事件循环」正是针对它的，
读通路重构后 `/alive` 应自然改善。验收判据：`/alive` p95 ≤ 100ms。

**另注：**空闲 60 秒后的首击并不慢（1.5ms），所以这不是冷启动问题。

## 六、技术债：strict mode 把 1 个必填字段变成 21 个

**现象。** `_run_cognition` 抛 `ValueError('本轮返回结构无效，原始消息和实际回执已保留。')`。

**量化（24 小时）：**

```
最终失败 13 次 / 认知轮次 670 = 1.94%
按小时：Nov18 22点 2次 / 23点 5次 / Nov19 01点 2次 / 15点 1次 / 19点 1次 / 21点 2次
```

重试是**同一轮内自动的**（`for correction in range(2)`），所以 1.94% 已经是"重试后仍失败"。
原始失败率约 3.9%，重试救回约一半。

**只能追到 6/13 次的根因**（其余 journal 已轮转，只剩"结构无效"一行）——这一点如实标注。

**三种形态（可追溯的 6 次）：**

| 形态 | 次数 | 校验位置 |
|---|---|---|
| A 多写键 | 1 | `additionalProperties` in `drive_feedback.items`（模型多写 `'project': ''`） |
| B 缺必填 | 4 | `'reply' is a required property` |
| C 类型错 | 1 | `type` in `properties.followups` |

**根因一层。** `life/companion_model.py:39 strict_schema()` 给所有 `type == 'object'` 的节点强制：

```python
result['additionalProperties'] = False
result['required'] = list(result.get('properties', {}))
```

这是 OpenAI strict function calling 的硬要求（`life/companion_model.py:133-134` 发 `strict=True`），
**不能改**。

**根因二层（关键）。** 手写的 `DECISION` 顶层只有 1 个必填键：

```
手写 required:            ['reply']
strict_schema 后 required: 21 个（reply + share_town, share_bilibili, silence_pending,
                           work_mode, delivery, images, followups, remember, projects,
                           actions, wake_after, letter, speak_body, scene, communication,
                           commitments, reports, drive_feedback, motivation_choices, engagement）
```

**所以模型必须同时吐 21 个顶层字段，漏掉任何一个就整轮失败。
而形态 B 那 4 次漏的恰恰是 `'reply'`——手写 schema 里唯一本来就必填的那个。**

完整清单（实测扫描，8 个节点共 27 个"原本可选"被变必填）：
`projects.items`（blocked_on/checkpoint/close_reason/evidence_refs/next_step/origin/project_ref/review_after/status）、
`actions.items`（arguments/command/cwd/goal/op）、`commitments.items`（next_step/recover_when/review_after/status/waiting_for）、
`reports.items`（basis_refs/project_ref）、`communication`（basis_refs/topic_ref）、
`remember.items`（kind/reason）、`engagement`（project/reason）。

**需要设计决策（设计文档目前没覆盖 strict mode 这一块）：**

> strict mode 把 27 个可选字段变必填，实测三种失败形态，
> 代价是 **1.94% 的认知轮次完全失败**。
> 需要决定：接受，还是重构 schema 结构（例如拆成两次模型调用）？
> 若要重构，注意别把"减少必填字段"变成"减少表达能力"。

**修法候选：**

```
D. 拆成两次模型调用（先出 reply+actions+communication，再出 drive_feedback 等长尾）
   代价：每轮多一次模型调用
E. 在 schema description 里逐字段写明"没有就填 null / 空数组"
   代价：治标
F. 白名单式清理已知多余键（只删 drive_feedback[*].project）
   代价：最小，但每遇到一种新形态就要加一条
G. 接受（当前状态 1.94%）
```

**建议：先积累"多余/缺失键"的样本。若出现 3~4 种稳定形态，再按 F 加白名单；否则维持 G。**

## 七、今天被撤回或纠正的判断（给下一个人：这些坑在哪）

**结论层面的撤回：**

| 判断 | 结果 | 如何被推翻 |
|---|---|---|
| `index_access_fence IS NULL` 造成死锁 | 推翻 | 实调 `claim_next` 5/5 都拿到 lease |
| N+1 查询是 recall 慢的主因 | 推翻 | 写了批量版，实测比 N+1 **更慢**（0.7x） |
| 计算占 69% | 推翻 | 那是被 `scan_limit` 截断后的隔离测试，两边都扫到 10001 片就退出 |
| `scan_limit` 导致语义召回返回 0 条 | 撤回 | 生产探针证明 `search_vectors` 从未被调用 |
| `query_vector()` 慢导致超时 | 推翻 | 实测 `query_vector` 中位 1ms（缓存命中），8231 直连 60ms |
| `allowed()` 被调 3 次 | 推翻 | 实测只调 1 次，但它自己占 62% |
| 空表 `life_acl` 可短路成"全部允许" | **被自己查证拦下** | `_allowed` 后面还有三条早退判断，短路会改变权限语义 |
| "切 text 让她更慢"（37s vs 20s） | 推翻 | 两次她做的事不同；37 秒里 32 秒是她在读 B 站字幕 |
| 误报"30 分钟 6 次结构无效" | 纠正 | grep 的是行数，每次失败打 3 行；实际 2 次 |
| "3.4% × 3.4% ≈ 0.1%" | **纠正** | 重试不是独立事件（同一上下文），救回约一半，实际 1.94% |

**方法层面的教训：**

```
1. 测 A 和 B 的差别，先确认两边都跑到了终点（空结果不等于"一样"）
2. 复合概率不能假设独立事件
3. 报"多少次"先确认每次失败打几行
4. 报"耗时"必须带样本数、min/中位/p99/max、当时的负载
5. 任何"应该多快"的假设，都要在当前时刻重测，不能用几小时前的数
6. 优化前先确认那段代码在生产里真的被执行了
7. 查"系统变慢"，先看它在忙什么，而不是先猜是谁让它慢
8. 权限函数不许为性能精简分支
9. 函数体内的错误，`ast.parse` 和 `import` 都抓不到，要用空库跑一次真实调用
```

## 八、本阶段没做的

- `RecallPacket` 参数化（设计说这是前半之后的最终形态，留给召回管线重构）
- 真实 dialogue 轮验证（需要一个真实用户对话轮；当晚无对话，故未观察）

## 附录：`ops/scripts/recall_counts.py`

见同提交内的该文件，用法：

```
python ops/scripts/recall_counts.py --since-min 60
```

输出合资格分母、跳过、超时、成功运行、找到匹配、实际装入上下文六个计数。
