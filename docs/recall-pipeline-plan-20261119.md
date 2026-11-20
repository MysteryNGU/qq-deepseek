# 设计 §8.2 召回管线：最小改动清单（待批准，未动代码）

对应设计：`docs/superpowers/specs/2026-11-19-open-issues-resolution-design.md`
第 3.1 / 3.2 / 3.3 / 4 节。**本文件只是计划，不含任何代码改动。**

## 〇、现状核实（写清单前实测，不是照抄设计）

```
Repository：单连接 sqlite3.connect(check_same_thread=False) + threading.RLock（life/repo.py:42,45）
             所有 _all/_one/_exec/tx 都 with self._lock → 全局串行
主线程占用：py-spy 45s/2523 样本，Repository._all 37.9% + _one 17.8% = 55.7%，idle 仅 2.3%
search() 结构（life/life_recall.py）：
  202-216  candidate_refs 计算 → allowed()          ← 先做全量质量+权限
  225      text_route（life_terms）
  226      search_vectors（逐资源 N+1）
  233      find_related
  255      ranked 合并排序                             ← 排序在最后
  261-281  逐条 access.load（读正文）+ 预算裁剪        ← 正文在最后
  291      _cursor() 写 meta 快照                     ← 读路径里有写
依赖 self.repo 的位置：life_recall.py / life_catalog.py / life_access.py 合计 70 处
```

设计 §3.1 要的顺序是「**先排序候选 ID → 再权限精查 → 只加载通过验证的正文**」，
现状是「**先全量 allowed（授权全部）→ 再排序 → 再加载正文**」。顺序相反。

## 一、改哪几个文件、每个改什么

| # | 文件 | 改什么 | 新/改 |
|---|---|---|---|
| 1 | `life/recall_session.py` | 新建 `RecallReadSession`：专用 worker 里的只读 SQLite 连接（`PRAGMA query_only=1`），短读事务，绑定自己的 `Repository` 视图 + `LifeCatalog` + `LifeAccess` | **新** |
| 2 | `life/life_recall.py` | ① 新增 `search_v2()`（不动 `search()`）；② `allowed()` 增加 `effects_of()` 轻量版——先只算**候选集**的 ACL；③ `_cursor()` 在 v2 里不写，改为不产出 cursor | 改 |
| 3 | `life/memory_index.py` | ① 新增 `rank_candidates(allowed_ids, query_blob)`：只返回 `(ref, score)`，不加载正文；② `search_vectors` 不动 | 改 |
| 4 | `life/companion.py` | `_prefetch_recall` 改调 `search_v2`，走 `RecallReadSession`，保留现有 timeout 与合作取消 | 改 |
| 5 | `ops/scripts/recall_oracle.py` | 旧实现 vs 新实现的差分对照脚本 | **新** |

**不动**：`Repository` 本身、`life_catalog.py`、`life_access.py` 的公开语义（只在 1 里包一层只读视图）。

## 二、先后顺序（哪些是前置）

```
P1  RecallReadSession（文件 1）+ 用它能跑通一次只读 SELECT
    ↓ 前置：没有它，后面所有测量都还在主锁里
P2  候选生成器：rank_candidates（文件 3）+ effects_of（文件 2-②）
    ↓ 前置：必须先能"只要 ID 和分数"，才谈得上"先排序后精查"
P3  search_v2() 组装新管线（文件 2-①③）
    ↓ 前置：P1+P2 都在
P4  差分 oracle 脚本（文件 5）
    ↓ 必须在 P5 之前完成并能跑
P5  companion 切到 search_v2（文件 4）
    ↓ 只有 P4 通过才做
P6  观察 /alive p95 与召回 p95，按设计 §9 的 feature flag 回退
```

**必须做（P1~P4）**：不给 oracle 就不许切。
**可后做**：ANN（设计 §3.2 明说"若紧凑缓存仍不够再评估"）、
向量内存缓存、跨请求缓存授权结论（设计 §3.3 明令首版禁止）。

## 三、新旧怎么并存

```
life_recall.search()      保持原样，不删不改签名  → 旧实现（oracle）
life_recall.search_v2()   新管线                 → 新实现
companion._prefetch_recall 通过一个显式开关选择：
    RECALL_PIPELINE=legacy | v2   （默认 legacy）
```

**开关默认 `legacy`。** 只有 oracle 通过、且人工确认后才设 `v2`。
回退只需删环境变量。

`search_v2` **不产出分页 cursor**（设计 §4：对话预取首版不生成持久分页游标）。
需要分页的显式检索继续走旧的 `search()`。

## 四、怎么证明新实现和旧实现一致

### 4.1 用什么 query

```
来源：events 表最近 7 天、payload 里可读文本长度 ≥ 20 的 20 条（t43 已建的
      tests/golden_recall_20261119.json 就是这一份，可复用）
补充：3 条人工构造的边界 query —— 空串、纯符号、超长（1999 字符）
```

### 4.2 比什么字段

```
对每个 query、每个 mode ∈ {text, semantic, related, auto}：
  ① 返回条数一致
  ② 每条 resource_ref 的顺序一致
  ③ 每条 score 误差 ≤ 1e-9
  ④ coverage 字典一致
  ⑤ match_routes 的集合一致（不要求顺序）
```

**不比对**：`next_cursor`（v2 不产出）、耗时。

### 4.3 怎么确认对照组的输出不是空的（t42 的教训）

**t42 那次 `旧 vs 批量 结果一致=True` 是假的**——两边都因 `scan_limit` 截断返回 `([], 'partial')`。
所以差分脚本必须先做**非空断言**：

```python
# 每个 (query, mode) 组合，先断言旧实现真的产出了东西
assert len(old_items) > 0, 'OLD implementation returned nothing for %r/%s - comparison is meaningless'
assert old_coverage.get('semantic') != 'partial' or mode != 'semantic', 'truncated, cannot compare'
# 统计
nonempty_pairs = sum(1 for r in results if r['old_count'] > 0)
assert nonempty_pairs >= 0.8 * total_pairs, 'too many empty comparisons: %d/%d' % (...)
```

**判据：如果非空比例 < 80%，差分报告标 `INCONCLUSIVE`，不算通过。**

### 4.4 oracle 跑在哪里

**设计 §8.2 要求"旧实现作为隔离数据副本中的正确性 oracle"**——不在生产库上反复全扫。

```
1. sqlite3 .backup 生成 /tmp/recall_oracle_<ts>.db（WAL 下可在线备份，约 100MB）
2. oracle 脚本对该副本跑旧实现和新实现
3. 差分报告落 ops/reports/recall_oracle_<ts>.json
```

### 4.5 权限差分（设计 §3.3 明列）

```
五种场景各一组：
  空 ACL / 新增 deny / 删除 deny / 到期 / 祖先拒绝 / diagnostic / 跨 scope
对每组：旧实现与新实现返回的 resource_ref 集合必须完全相同
```

## 五、风险与不做的事

| 风险 | 处理 |
|---|---|
| 新实现在 `query_only=1` 连接上误写 | 只读连接会直接报错，不会静默写坏；P1 先跑一次写入尝试确认它确实拒绝 |
| 跨线程传 `sqlite3.Row` | 设计 §4 明令禁止；`RecallReadSession` 边界处一律转成普通 dict |
| 取消后仍占连接 | 设计 §4：CPU 循环检查 deadline，SQL 用 `interrupt()`；超时后必须回滚事务 |
| `/alive` 反而变差 | 设计 §4 说"`/alive` 仅检查存活，不构造 snapshot"；先核实现有 `/alive` 是否已如此 |

**明确不做：**
```
✗ 不复做 IN 批量（t42 实测更慢 0.7x）
✗ 不把空 ACL 解释成全开放（t43 已确认 _allowed 有三条早退）
✗ 不在首版加跨请求授权缓存（设计 §3.3 明令）
✗ 不为"减少必填字段"动 DECISION schema（那是另一条线，见 strict mode 技术债）
```

## 六、我需要你决定的三个点

```
① P1（RecallReadSession）要不要先单独做一小步、单独部署？
   好处：能先测出"换只读连接能省多少"，再决定后面值不值得做
   坏处：多一轮部署

② oracle 的数据副本用 .backup 还是先导出子集？
   .backup 是最真实的；但 100MB 会占磁盘，且导出要时间

③ 一致性的严格程度：
   text/semantic/related 三条路线都要求逐条顺序一致，
   还是只要求"集合一致 + top-10 内顺序一致"？
   前者严，但新管线若调整了分数融合，可能永远做不到完全一致
```
