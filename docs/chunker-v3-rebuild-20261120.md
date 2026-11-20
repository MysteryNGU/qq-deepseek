# 索引切块爆炸（chunks bug）与 v3 重建 — 2026-11-20 夜间

对应提交：`ea852a8`（chunks 修复 + v3 + 速率控制）、`3d85998`（3 处调用方）
前序：`docs/p0-recall-fixes-20261120.md`、`docs/superpowers/specs/2026-11-19-final-issues-amendment-design.md`

## 一、bug 本体

`life/memory_index.py::chunks()`

```python
if end < len(text) and last_boundary and last_boundary > start:
    end = last_boundary
```

`last_boundary` 只保证**大于 start**，不保证**超过上一块的终点**。
当窗口内最后一个边界落在很靠近 start 的位置时，`end` 被拉回那里，
overlap 回退几乎退不动，于是 `start = max(start+1, overlap_start)` **每次只推进 1 个字符**。

### 最小复现（10 秒可看）

```python
text = '句号在这里。' + '没有标点的连续文字' * 200      # 1806 字符
```

修复前逐 step：

```
step  start   end  last_bound  piece_len  推进
   0       0      6          6          6     6   ← 拉回 250
   1       1      6          6          5     5   ← 拉回 251
   2       2      6          6          4     4
   3       3      6          6          3     3
   4       4      6          6          2     2
   5       5      6          6          1     1   ← 每次只推进 1 字符
```

**触发条件**：窗口内最后一个边界距 start 很近（此例 6 字符），
且其后到窗口末尾（250 字符）没有任何标点。
**不会触发**：完全无标点的文本（`last_boundary=None`，不拉回）。

### 修法

```python
prev_end = -1
while start < len(text):
    start_before = start
    ...
    if end < len(text) and last_boundary and last_boundary > prev_end:
        end = last_boundary
    ...
    start = overlap_start if overlap_start > start_before else end
    prev_end = end
```

边界必须严格超过上一块的终点；overlap 若不能向前推进，直接跳到 `end`，不原地打转。

## 二、影响面（实测）

### 块数分布（v2，75185 行）

```
块数区间      资源数   资源占比   块数占比
1-3           7967     85.5%     12.8%
4-10           346      3.7%      3.0%
11-50          413      4.4%     15.3%
51-100         519      5.6%     54.0%   ← 5.6% 的资源占 54% 的块
101-500         71      0.8%     14.9%
总块数 72171，平均 7.7 块/资源
```

### 块拼回原文：覆盖率 100%，**不丢内容**

```
life_ce85d913  463 块  原文 14189 字节  拼回 14189  覆盖率 100.0%（重叠 17181 字节）
life_7c691fed  462 块  原文 14100 字节  拼回 14100  覆盖率 100.0%（重叠 17118）
```

**结论：这是效率问题，不是正确性问题。检索不会漏内容，只是切得碎、结果同质。**

### 修复后（分层抽样，8806 个有索引的 event 资源，每层 15 条）

```
层        层规模   旧块/资源  新块/资源  压缩比
1-3块     7491      1.0        1.0      1.0x   ← 74% 的资源本来就不膨胀
4-10       330      6.5        3.7      1.8x
11-50      375     28.1       11.5      2.5x
51-100     536     70.0        7.7      9.1x
101+        74    143.1       11.9     12.0x

全量：72171 片 → 约 18015 片（4.0x）
平均：7.8 块/资源 → 2.0 块/资源
```

### scan_limit 覆盖率

```
修复前：10000 片 ÷ 7.8 块 = 只覆盖 1994 个资源（共 9320 个有索引的）→ 21.4%
修复后：10000 片 ÷ 2.0 块 = 约 4888 个资源 → 28.9%
```

**注意：不是"接近 100%"。按 16922 个 active 资源算，10000 片仍只覆盖约 29%。
膨胀收敛到 4x，不足以让 scan_limit 不再成为瓶颈——scan_limit 本身也要按设计 §6 处理。**

### 切分耗时同步下降

```
life_ce85d913  v2: 440 块 / 308.5ms    v3: 26 块 / 20.2ms
life_030c9893  v2: 496 块 / 351.0ms    v3: 25 块 / 19.4ms
```

`chunks()` 是同步纯 Python 循环，直接占事件循环。20 job 实测里
v3 的 `chunk_ms` 平均 **1ms**（p90 2ms），几乎可以忽略。

## 三、chunker_version → v3

```python
_est_src = inspect.getsource(estimate_tokens)
self.chunker_version = 'utf8-paragraph-v3-drop-blank:'+sha(encode(
    [self.config.chunk_tokens, self.config.overlap_tokens,
     estimate_tokens.__module__, sha(_est_src)]))[:12]
```

**为什么把估算函数纳入指纹**：`life/text.py:42` 有一个同名但语义不同的
`estimate_tokens`（汉字计数 vs 字节/2，差 1.82 倍）。
若谁改了 import，索引会静默按另一种度量切分，版本号却不变。

## 四、数据迁移

8131 条 v1/v2 pending job 全部标 superseded：

```
superseded_by      = 'v3'
superseded_reason  = 'chunker_bug_boundary_rollback'
superseded_at      = <时间戳>
superseded_from    = 原 chunker_version
status             = 'done'
```

**没标 `cancelled`（会污染统计），没删（丢证据）。**
拆分：v2 7463 条 + v1 668 条。其中 661/668 的 v1 job 已被 v2 索引覆盖；
7 条对应资源完全没有索引（会由 v3 全量重建覆盖）。

## 五、速率控制

```python
# 改前
INDEX_BATCH_PER_ROUND = 2
await asyncio.sleep(min(60, 5*(2**max(0,idle-1))) if idle else 5)

# 改后
INDEX_BATCH_PER_ROUND = 8
if idle:
    await asyncio.sleep(min(60, 5 * (2 ** max(0, idle - 1))))      # 没活时仍退避
else:
    await asyncio.sleep(2 if _process_cpu_percent() > 60 else 1)   # 有活时按 CPU 自适应
```

**实测速率**：

```
改前：5.2 条/分（生产）
改后：启动初期 26~66 条/分（CPU 15~45%，平均约 43）
      稳态 25~28 条/分（CPU 61~62%，降速已生效）
```

**"43 条/分"是启动初期的数，稳态是 25~28。报数时必须说明是哪个阶段。**

## 六、一个未修的设计缺陷：新产生的资源最晚才入队

### 现象（实测，恢复索引 40 分钟后）

```
小时            资源数   v3覆盖   覆盖率
01:45-00:45     204       2      1.0%   ← 最近 1 小时几乎为零
00:45-23:45      26      26    100.0%
23:45-22:45     359     359    100.0%
22:45-21:45     345     196     56.8%
21:45-20:45     307       0      0.0%
```

### 根因

```
最近 1 小时的 204 个资源 → 一个 job 都没有（job 状态分布 {}）
pending 抽样 400 条 → 全部是 3-6 小时前的资源
scan 游标 → 66 小时前，passes=0
```

`scan_enqueue` 按 `occurred_at DESC` 从"最新"往下扫。
索引恢复那一刻存在的资源按倒序依次入队（所以 22:45-00:45 已 100%）；
**但恢复之后新产生的资源，要等游标走完一轮（passes+1）才会进队列。**

### 影响的真实边界

```
① text 路线（life_terms 1208098 行）不依赖索引 → 刚发生的事**能被词法检索到**
② 只有"同义不同词"的语义匹配受影响
③ 一轮走完（约 10 小时）后恢复正常节奏
```

**准确的表述是"用词义匹配记不住刚发生的事"，不是"记不住刚发生的事"。**

### 候选改进（明天做，先查钩子）

```
A. 每轮 scan 先跑"最近 N 小时"的小查询（limit 20），再按游标扫历史
   → 改动小（加一个 query），但每轮多一次 DB 查询
B. 新资源产生时立即入队（事件驱动）
   → 更根本，但先要确认 life_resources 创建处有没有钩子
```

## 七、覆盖率数据里两个容易误读的地方

**① "3 小时 65.7% > 6 小时 44.2%" 不矛盾。**
3 小时窗口包含了"索引恢复那一刻已完成"的 22:45-00:45 段（100%），
而 6 小时窗口额外包含了尚未建的那部分，把比例拉低了。

**② 块拼回原文 100% ≠ 索引能用。**
拼得回只说明"没丢内容"；能不能检索到还取决于 `scan_limit` 覆盖面和
`chunker_version` 过滤（见下条）。

## 八、`search_vectors` 按 chunker_version 严格过滤

```python
# life/memory_index.py:374-376
"SELECT * FROM life_semantic_index WHERE resource_id=? AND agent_id=? AND model_key=? "
"AND chunker_version=? AND source_version=? AND state='ready' ORDER BY byte_start"
```

**所以 v3 重建期间，v1/v2 的索引行不参与检索。**
"v1/v2 还在被使用，至少能搜到"是错的。
语义召回覆盖率 = v3 覆盖率（重建初期 2.5%，恢复 40 分钟后 3.4%）。

## 九、流程教训

```
1. 提交前先对比 git diff --numstat 和 git diff -w --numstat
   差距大 → 行尾/空白噪声，不是真重写
   （people.py 显示 272/273 行全改，-w 后只有 2/1 行）

2. 怀疑 → 验证 → 结论，不要"结论 → 补验证"
   今晚两次栽在这上面：
     · "其他开销占 80%"（用生产 5.2 条/分反推，隔离实测是 embed 占 97%）
     · "7957 条领不到"（推断 claim_next 挡住，实测 claim_next 完全正常）

3. 报速率必须说明是哪个阶段
   启动初期 43 条/分、稳态 25~28 条/分，差 1.6 倍

4. 单机隔离实测 ≠ 生产实测
   隔离 31 条/分，生产 5.2 条/分，差 6 倍来自争用

5. 修复的收益要用分层抽样估，不能用 top-N
   top-20 给出"降 93%"，分层抽样给出"降 75%"（4.0x），差 4 倍
```

## 十、当前状态与明天的事

```
索引正在重建 v3：pending 约 6800 条，速率 25~28 条/分，约 10 小时
CPU 61~62%（降速已生效），/alive p50 多数 <20ms，WARNING/ERROR 0 条
v1/v2 索引行暂留，v3 建完后清理
```

**明天处理：**
```
① 新资源入队缺陷（先查 life_resources 创建处有没有钩子 → 定 A 还是 B）
② unknown 5 条（RECONCILE_UNKNOWN_ENABLED=False，要单独开）
③ running 4 条（租约过期，attempts 最高 5，其中一条 error=idempotency_conflict）
④ 看 v3 覆盖率、稳态速率、/alive p95
```
