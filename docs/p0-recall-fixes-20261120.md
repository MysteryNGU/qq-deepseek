# P0 召回修复（2026-11-20，对照修订版设计）

对应：`docs/superpowers/specs/2026-11-19-final-issues-amendment-design.md` §4/§5。
提交：`31d1d32`、`6ef07b4`、以及本次 `text 显式跳过 search_vectors`。

## 一、P0 三件（已交付）

### ① 非分页 context（`include_cursor`）

**问题。** `packet()` 无条件构造 `next_cursor`——一个签名的 base64 分页游标。
生产绑定的空结果骨架实测 **1154 token**，绝大部分就是它。
`budget < 1162` 时，无论有没有命中都抛 `budget_requires_page`，
而 `context()` 把它静默吞成 `''`。

**改法。**

```python
def search(..., include_cursor=True):
    ...
    next_cursor = None
    if include_cursor and pending:
        next_cursor = self._cursor(...)
```

`context()` 内部传 `include_cursor=False`；`RECALL_CONTEXT_CURSORLESS=0` 可单独关掉新路径。

**配套（修订版 §4 要求）。**

- `include_cursor=False` 时拒绝传入 cursor：实测 `cursor='abc'` → `RecallError('invalid_input')`
- 不写快照：`state_snapshots` 在两种路径下均为 4133 → 4133

**效果。** 生产规模（allowed ≈ 10249）下 `context(budget=800)` 从恒返回 `''` 变为返回 **695 字符**。

**局限（不掩饰）。** 空结果骨架仍 **919 token**，远高于设计希望的 200；
单次 `context()` 仍 **800~3400ms**，因为每次要发 2.2~3.4 万条 SQL。这两项是结构问题，留给召回读通路。

### ② `ignore_mode_override` 全链路

**问题（我引入的 bug）。** `search_async` 解析出 mode 后传给 `search`，
`search` 又 `resolve_mode(mode)` 一次且不带绕过标志 → 环境变量二次覆盖。

```python
# 实测：环境 text + 请求 semantic + ignore=True
diagnostics.mode = 'text'      # 期望 semantic
```

**改法。** `search()` 加私有 `_mode_resolved`；`True` 时不再解析。`search_async` 设它。
直接调 `search()` 的旧调用方不受影响。

**四类断言（修订版 §5 要求）。**

```
① 普通入口受环境覆盖     auto→text ✓   semantic→text ✓                 PASS
② 诊断入口可绕过         semantic→semantic ✓   related→related ✓       PASS
③ text 无 embedding      qv=0、向量 SQL=0                              PASS
④ 诊断 semantic 真走语义  qv=1、coverage.semantic=partial、向量 SQL=2322  PASS
```

**关键：第 ④ 条此前从未通过过。** 只测 `resolve_mode` 的六个样例替代不了整条调用链。

### ③ 统一 `routes` 统计契约

**问题。**

```
route_counts = {resource_ref: 路线数}     ← 不是每路线命中数
_prefetch_recall: trace['routes'] = diagnostic.get('routes')
                  ↑ 该键当时根本不存在 → 落库的 routes 恒为 {}
```

**所以我今天落库的那个字段，`routes` 部分一整天都是死的。**

**改法。** `search()` 的 `diagnostics` 增加统一 `routes` 对象，每路线含
`attempted / status / candidate_count / returned_count`；旧键保留兼容。

**实测输出。**

```json
{"text":    {"attempted": true,  "status": "ready",       "candidate_count": 40, "returned_count": 1},
 "semantic":{"attempted": false, "status": "unavailable", "candidate_count": 0,  "returned_count": 0},
 "related": {"attempted": true,  "status": "ready",       "candidate_count": 0,  "returned_count": 0},
 "exact":   {"attempted": false, "status": "unavailable", "candidate_count": 0,  "returned_count": 0},
 "mode": "text"}
```

### ④ text 显式跳过 `search_vectors`

**问题。** `search()` 无条件调用 `search_vectors(allowed, query_vector)`。
`query_blob` 为空时它第一行就 `return [], 'unavailable'` —— **一条 SQL 都不发**，
但函数被调了一次，而且返回 `'unavailable'`：在统计口径里等于"后端故障"，
实际是"这个模式不走向量"。修订版 §5 要求区分 `skipped_mode` 与 `unavailable`。

**改法。**

```python
if query_vector is not None:
    semantic, coverage['semantic'] = self.index.search_vectors(allowed, query_vector)
else:
    semantic, coverage['semantic'] = [], 'skipped_mode'
```

**实测。**

```
text      qv=0 sv=0 向量SQL=0     status=skipped_mode
semantic  qv=1 sv=1 向量SQL=2322  status=partial
related   qv=0 sv=0 向量SQL=0     status=skipped_mode
```

## 二、修订版 §2 的七条纠正，逐条对照

| # | 我原来的判断 | 修订版纠正 | 我的实测 |
|---|---|---|---|
| 1 | `context(budget=800)` 无论是否命中必然失败 | 有剩余结果需分页时游标可能挤掉内容；**不是所有查询必然失败**。1162 是本实验绑定与 packet 的开销，不是函数通用下界 | **修订版对**。生产规模下（修复后）返回 695 字符。旧路径下确实抛，但那是本绑定的骨架尺寸所致 |
| 2 | 找记忆从来没工作过 | 该 context 分支有严重预算问题，但 `_messages` 还有 prior_users、history、8000 预算的 prefetch；不能推出全部记忆失效 | **修订版对**。我把一个分支的问题说成了整条链路的问题，表述过强 |
| 3 | `ignore_mode_override` 诊断已解决 | `search_async` pop 了标志，但 `search` 再次 `resolve_mode` 且不接收绕过标志 | **证实，已修**（见 ②）。这是我引入的 bug |
| 4 | text 完全不调用 `search_vectors` | 仍无条件调用，只因 `query_blob` 为空而快速返回 | **证实**：不发 SQL，但函数被调 1 次，且返回 `unavailable` 污染统计。已改为 `skipped_mode` |
| 5 | 日志轮转，原始返回丢失 | `_call_model` 已通过 `_audit` 把 content/tool_calls/request_id 写到 `companion_activity`；24 小时 808 条 decision 均有文本 content，17 条最终校验错误 | **未验证**。我此前说"6/13 找不到"是只查了 journal，没查 `companion_activity` |
| 6 | recall 统计只缺真实分母 | 还有字段契约错误：`search` 写 `route_counts`/`route_status`，`prefetch` 读 `routes`；`route_counts` 的键实际是 resource_ref | **证实，已修**（见 ③） |
| 7 | （未察觉）| `semantic_items` 数的是 prefetch 候选，不是装进上下文的条数；`recall_counts.py` 据此判断装入，且把 `semantic_unavailable` 算作运行成功 | **证实，未修**（脚本仍未改口径） |

**一句话总结：我那 7 条里 5 条成立、1 条表述过强（#2）、1 条是彻底错的（#1 的"必然"）。**

## 三、`/alive` p95 的三个数（**注意：不要记成"我们的优化有效"**）

```
部署前基线（含旧路径）   p95 = 1155.7ms
部署 include_cursor 后    p95 =  954.8ms
部署三件补齐后            p95 =  388.5ms
```

**p50 一直是 1.5~3.1ms。**

**这个"下降"不能归因于改动。** 三个数分别来自三个不同时刻的采样，
中间各有一次服务重启。第二次重启（2355740）比第一次更干净——
缓存状态、索引写入进度、后台任务队列都不同。

**唯一能说的是：这一轮改动没有让 `/alive` 变差。**
红线①是 `p95 ≤ 100ms`，**三个数都远未达标**，它属于固有问题
（py-spy 实测主线程 55.7% 在同步 DB，idle 仅 2.3%），解法在设计 §3/§4 的读通路重构。

## 四、这一轮的方法教训

```
1. 加参数之前先确认它影响的是哪一段数据
   include_provenance 想裁 item 字段，而骨架 items=[] 里根本没有 item → 919→920，无用，已回退
2. 统计字段要验证"每个字段都有值"，不能只验证"记录写出了"
   routes 字段落库一整天都是空的，因为读的键名根本不存在
3. 诊断开关要测整条链路，不能只测被开关的那个函数
   ignore_mode_override 只测了 resolve_mode，而故障出在下一层又解析了一次
4. 签名簇（skipped_mode / unavailable）混用会污染统计
   前者是"我们决定不走"，后者是"走了但后端坏了"
5. 服务重启会改善看起来"卡"的指标，不能把重启效应记成优化效果
```

## 五、未做（按修订版 §6/§7 留给第二批）

```
scan_limit=10000 截断：仍 return [], 'partial'（整批丢弃）
  修订版 §6 要求退出整个扫描、返回已排序 top-K 并标 partial + scan_limit 原因
召回读通路（RecallReadSession / 候选优先 v2）：未开始
recall_counts.py 口径：仍把 semantic_unavailable 算成功、仍用 semantic_items 判装入
失败证据关联：未去 companion_activity 找回历史样本
strict mode schema：未动
golden：仍只有一份 INCONCLUSIVE 的 v1，未按修订版 §7 拆兼容集/修复集
```
