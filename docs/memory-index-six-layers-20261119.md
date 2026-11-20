# 记忆索引的六层死锁（2026-11-19）

## 一句话

一个「永远不工作」的功能，可能是六层 bug 叠加。每修好一层，下一层才露出来。
而每一层的错误信息都指向别处，所以「修好一处就宣布成功」会连续失败六次。

## 症状（修复前）

```
1000+ 条记忆没有索引
台账里 unknown 723 / paused 93，done 长期不动
v2 索引行涨得很慢，pending 越积越多
```

---

## 第一层：`unknown` 不看 `expires`

**症状**：同一个 job 反复被挡回 `unknown`，永远不重算。
样本：974/979 条已过期 238296 秒（66 小时）仍被判「未完成」。

**根因**：`life/embedding.py:129`（当时）无条件返回 `unknown`，
而记录本身带 `expires`（`created + 3600`）。

```python
if saved['state'] not in {'completed','evicted'}:
    return dict(state='unknown', ...)      # 不看 expires
```

**修法**：窗口内不重试（尊重幂等与计费），窗口外放开，由 `generation+1` 走新的 `billing_key`。

**验证**：过期记录重算后 `generation` 进位。

---

## 第二层：`reconcile_unknown` 不存在

**症状**：`memory_index.py` 的注释承诺「unknown/paused jobs are reconciled separately」，
但全仓库没有任何 `memory_index` 的 reconcile 实现。

**根因**：注释写了契约，实现没跟上。

**修法**：补了 `reconcile_unknown()`，但用开关 `RECONCILE_UNKNOWN_ENABLED = False` 关着
（`life/companion.py:179`），因为它是下游，前置条件没满足前放开等于制造新的积压。

**现状**：仍然关着。开它需要先满足第一、三、四层的条件。

---

## 第三层：`claim_next` 要求 `index_access_fence IS NULL`

**症状**：815 条 pending 一条都领不到。

**根因（当时的判断）**：`enqueue_resource` 写 `index_access_fence`（总是非空），
`claim_next` 要求 `IS NULL`，两处契约不一致。

**重要更正（2026-11-19 10:25）**：
**这条判断我后来推翻了。** 直接调生产代码的 `claim_next` 连测 5 次，5 次都领到。
「读 SQL 里的 `IS NULL` 就断定会挡住全部」是错的——实际运行行为与静态阅读不一致。

所以**第三层不是死锁**。我的修正尝试（改 `IS NULL` 判断）已回滚，没留下副作用。

**教训**：能不能跑，就跑；不能跑，才读代码，并且要把「这是推演」标出来。

---

## 第四层：HTTP 400 不释放预算预留

**症状**：job 被打成 `paused`，error = `idempotency_conflict`，10 分钟内从 0 涨到 72。

**根因**：`life/embedding.py` 的异常分支只处理 `ConnectionRefusedError`：

```python
if isinstance(reason, ConnectionRefusedError):
    self.spending.settle_external(billing_key, 0)     # 只有这条
# HTTPError 走 unknown 分支，不 settle
```

而 `life/spending.py:247` 的判重是 `SELECT 1 FROM spending_calls WHERE call_key=?`
——**不看 state**，而 `settle_external` 只把行 UPDATE 成 `settled`、不删行。
所以 key 一旦预留过就永久不可复用。

实测残留：72 条 paused 全部对应 `state='reserved'` 且 `reserved=0` 的账。

**修法**：
- 4xx → `settle_external(billing_key, 0)` 释放钱
- 抛 `embedding_rejected_input`，**判 terminal 不重试**（400 是客户端算错，重试不会自己好）

**为什么不写 `state='evicted'`**：`generation` 只在「state 不在 `{completed,evicted}`」时 +1，
写成 evicted 会让 `billing_key` 永远停在原值。

---

## 第五层：`generation` 自锁

**症状**：清理完 `spending_calls` 后，`done` 仍然 0 增长，`paused` 继续涨。

**根因**：

```python
try:
    self.spending.reserve_external(billing_key, ...)   # ← 在这里抛
except SessionFailure:
    raise RecallError('embedding_budget_unavailable')
saved = dict(..., generation=generation, ...)           # ← 永远走不到
self.repo._exec('INSERT OR REPLACE INTO meta ...')
```

**闭环**：要推进 `generation` 必须先 `reserve` 成功；要 `reserve` 成功必须先推进 `generation`。

**修法**：`reserve` 冲突时直接把 `generation+1` 落盘并抛 `embedding_generation_advanced`
（列入 retryable）；加 `MAX_GENERATION = 5` 上限，超过抛 `embedding_generation_exhausted`（terminal）。

---

## 第六层：一个 `"\n"` 的 chunk 让整批 400

**症状**：一个 4300 字的资源切 71 片、分 3 批：
第 1 批 32 条 OK、第 2 批 32 条 OK、第 3 批 7 条 HTTP 400。

**根因**：

```python
# life/local_embedding.py:113（当时）
any(not isinstance(t,str) or not t.strip() for t in texts)
```

第 3 批里第 0 条是 **1 个字符 `"\n"`**，`"\n".strip() == ""` → 被判空文本 → **整批 400**。
而 `chunks()` 按段落边界（`\n。！？!?；;`）切分时会切出只含换行的片。

**修法（两边都改）**：
- 客户端 `life/memory_index.py`：`chunks()` 不产出纯空白片（索引表 PK 是 `(...,byte_start,...)`，留空洞不影响读取）
- 服务端 `life/local_embedding.py`：空白文本单独报 `empty_embedding_text`，不再和 `invalid_embedding_input` 混在一起

**验证（curl 直打 8231）**：
```
{"input":["\n"]}     → {"error": "empty_embedding_text"}   HTTP=400   ✅
正常文本              → HTTP=200
40 条                 → {"error": "invalid_embedding_input"}
```

---

## 第七层（修完第六层才露出来的）：chunker 行为变了但版本号没变

**症状**：空片过滤后分片从 71 → 70，`fingerprint` 变了而 `state_key` 没变 → 撞 `idempotency_conflict`。

**根因**：`chunker_version` 只由 `(chunk_tokens, overlap_tokens)` 算出，
不含「是否跳过空片」这一行为差异。

**修法**：`utf8-paragraph-v1` → `utf8-paragraph-v2-drop-blank`，
让旧索引行不再算 ready，资源按 v2 重新入队。

---

## 性能：修好之后才谈提速

### 修复后的瓶颈不是算力

```
机器：nproc = 2，3GB 内存
embedding 进程：0.22 核
companion 进程： 0.15 核
合计：0.37 / 2 核 = 18.5%
```

### 8231 单条推理耗时与批大小无关

每档 5 次取中位数（每条 200 汉字）：

| 批大小 | 中位耗时 | 每条 |
|---|---|---|
| 1 | 204 ms | 203.7 ms |
| 2 | 462 ms | 231.0 ms |
| 4 | 939 ms | 234.7 ms |
| 8 | 2511 ms | 313.8 ms |
| 16 | 4092 ms | 255.8 ms |
| 32 | 7346 ms | 229.6 ms |

**每条恒定 200~310ms，与批大小无关。** 早先观察到「8 条比 16 条慢一倍」是采样噪声
（各次为 4491/2510/9303/1887/2099 ms，波动 5 倍）。

### 真瓶颈：循环尾部固定 `sleep 5`

```python
lease = index.claim_next(ttl=120)
if lease:
    await index.build(lease)      # 一轮一个
...
await asyncio.sleep(5)            # 每轮固定睡 5 秒
```

单轮实测：

```
轮1: scan 202ms + claim 113ms + build  881ms = 1196ms
轮2: scan 707ms + claim 177ms + build 8922ms = 9806ms
轮3: scan 163ms + claim  62ms + build  549ms =  774ms
轮4: scan 198ms + claim  63ms + build  158ms =  419ms
轮5: scan 164ms + claim  64ms + build  247ms =  475ms
```

大部分轮次 0.5 秒就干完，然后睡 5 秒。

### 修法：一轮处理 4 个，有活不退避

```python
INDEX_BATCH_PER_ROUND = 4
for _slot in range(INDEX_BATCH_PER_ROUND):
    lease = index.claim_next(ttl=120)
    if not lease: break
    await index.build(lease)
    worked += 1
idle = 0 if worked else min(idle + 1, 5)
```

顺手修了一个空转隐患：原来 `idle = 0 if scanned else idle+1`，
只要入队了但 `claim` 领不到就会一直 0 → 不退避 → 空转。

### 提速前后

| | 修前 | 修后 | 红线 |
|---|---|---|---|
| 速率 | 5 job/分 | 37.33 job/分 | — |
| 每 job | 12 秒 | 1.6 秒 | — |
| CPU | 18.5% | 16.0% | < 30% ✅ |
| `/alive` | 1.3 ms | 2.1 ms 中位 | < 10 ms ✅ |

发往 8231 的请求仍由 `_EMBEDDING_IO_LOCK` 串行化（8231 是单线程 `HTTPServer`，
`threads=2` 只喂给 `torch.set_num_threads`，不是 HTTP 并发）。

### 预计完成时间

**6.3 小时**（14261 条 pending v2 × 1.6 秒）。

---

## 816 条 v1 残留的处置

**不标 `cancelled`**：cancelled 的含义是「这件事不该做、被取消了」，
而这 816 条的真实含义是「chunker 升级，由 v2 接管」。

**实际处置**：标成 `done`，payload 记：

```json
{
  "superseded_by": "utf8-paragraph-v2-drop-blank",
  "superseded_at": 1795056000,
  "superseded_reason": "chunker 升级 v1→v2（跳过纯空白片），该资源的索引由 v2 job 接管重算",
  "superseded_from_status": "unknown"
}
```

**统计时必须区分**：

```sql
-- 真实完成数
SELECT COUNT(*) FROM work_jobs WHERE kind='memory_index' AND status='done'
  AND json_extract(payload,'$.superseded_by') IS NULL;
-- 被 v2 接管的历史账
SELECT COUNT(*) FROM work_jobs WHERE kind='memory_index'
  AND json_extract(payload,'$.superseded_by') IS NOT NULL;
```

前置核对：816/816 都满足「`chunker_version=v1`」且「对应资源已有 v2 的 pending/done/running job」。

---

## 收尾验收判据

| # | 判据 | 命令 |
|---|---|---|
| ① | pending 归零或接近零 | `SELECT COUNT(*) ... WHERE status='pending'` |
| ② | 816 条已按上面处置 | `SELECT COUNT(*) ... WHERE json_extract(payload,'$.superseded_by') IS NOT NULL` = 816 |
| ③ | v2 索引行 ≈ 资源数 × 平均片数 | `SELECT COUNT(*) ... WHERE chunker_version LIKE 'utf8-paragraph-v2%'` |
| ④ | 真实 done（减 816）≈ 资源总数 | 见上面的 SQL |
| ⑤ | **抽查 3 条：`memory op=search` 能查到内容，且内容真实** | 见下 |
| ⑥ | CPU < 30%、`/alive` < 10ms 持续 | `read_cpu` 十秒窗口 + `curl /alive` |

**⑤ 是关键：索引行数涨了不等于能查到。** 索引行和检索是两条路径
（写 `life_semantic_index` vs 读 `memory op=search`），前者通了不代表后者能返回内容。

---

## 教训

**每一层的错误信息都指向别处：**

```
unknown          → 说「结果未知」（实际可能是 400）
idempotency_conflict → 说「重复提交」（实际是上次没释放）
claim 领不到      → 说「没有 job」（实际是 fence 不匹配 —— 而且这条是误判）
HTTP 400         → 说「输入无效」（实际是其中一个字符是 "\n"）
```

**做法上的三条：**

1. **每修好一层，立刻用「逐步回执」验证**：chunks → 分批 → 逐批 embed → 真实 build → INSERT。
   `done` 涨没涨只告诉你终点，不告诉你卡在哪。
2. **进度可观测不是锦上添花**：第四层是「每 50 条记一条 audit」暴露出来的。
   没有它，我们会以为「合批修好了」就结束了。
3. **读代码推断 ≠ 实际运行**：第三层就是这么误判的。
   能跑就跑；不能跑才推演，并且要把「这是推演」写进结论本身。
