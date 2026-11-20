# 待上线的修复（2026-11-19 夜）

**改好了，但现在不能上——会打爆本地 embedding 服务。**

## 前提条件

```
本地 embedding 服务：http://127.0.0.1:8231/v1/embeddings
模型：BAAI/bge-small-zh-v1.5（CPU 推理，dimension=512）
超时：30 秒
计费：local-cpu-zero-provider-fee-v1

实测并发边界：
  串行 6 次     → 6/6 成功
  并发 2 路     → 一成功一失败交替（meta 里 unknown/HTTPError ↔ completed）

结论：安全并发数 = 1。
```

## 三处修复（都已写好、验证过，现在回滚等待）

### 1. `life/embedding.py:129` —— unknown 记录从不检查 expires

```python
# 原代码
if saved['state'] not in {'completed','evicted'}:
    return dict(state='unknown', vectors=[], input_digests=input_digests)
```

**unknown 记录本身带 `expires`（created + 3600），但代码不看它。**
**实测：meta 里 979 条 unknown，其中 974 条已过期 238296 秒（约 66 小时），仍被判为"未完成"。**

**修法**：`if saved.get('expires', 0) > self.life.clock():` 才返回 unknown，过期后放行重算。

### 2. `life/memory_index.py` —— `reconcile_unknown()` 原本不存在

```python
# memory_index.py:99 的注释
# "Materialize the generation's existing jobs once, including protected
#  unknown/paused jobs. They are reconciled separately, never requeued."
```

**"reconciled separately" 是全仓库找不到实现的空头承诺。**
**实测：unknown job 1007 条，`due` 已过期 251899 秒，`attempts=1`，没有任何机制处理它们。**

**修法**：新增 `reconcile_unknown(limit)`，把 `due` 过期且 `attempts<3` 的 unknown 改回 pending。
**实测有效**：unknown 1007 → 314，pending 0 → 701。

### 3. `life/memory_index.py:189` —— `claim_next` 的 fence 条件与 enqueue 相反

```python
# enqueue_resource(:94)  写入
index_access_fence = self._access_fence()      # 总是非空

# claim_next(:189)  要求
"AND json_extract(payload,'$.index_access_fence') IS NULL "   # 永远不成立
```

**`_access_fence()` 的语义（life_catalog.py:38）：**
```
"""Authority changes invalidate computation; unrelated new records do not."""
return sha(encode([self.epoch('policy'), forget_epochs]))
```
**它是"权限版本号"，不是"有没有围栏"的布尔量。`_validate()`（:146）正确用法是比较当前值。**

**实测逐步放宽 WHERE：**
```
scope+kind+status              → 836
+ index_access_fence IS NULL   → 0      ← 全被挡住
```

**修法**：改成 `IS NULL OR = ?`（传当前 `_access_fence()`）。

## 为什么现在不能上线

**三处都修好之后：**
```
1026 条积压同时可领
→ claim_next 的重 SQL（json_extract + NOT EXISTS）每轮都跑
→ 领到就 build
→ 8231 并发失败（50%）
→ CPU 从 6% 涨到 79.8%
```

**py-spy record 20 秒 4432 样本，2562 个落在 `MemoryIndex.claim_next`。**

## 上线前提（按顺序做完才能开）

```
1. 给 _index_memories 加每轮上限（一轮只 build 1-2 条）
2. 失败的 job 退避到分钟级（不是现在的 60 秒）
3. 或者把 8231 换成能并发的（多 worker / GPU / 更快的模型）
4. 再放开 fence 和 reconcile
```

## 当前状态（2026-11-19 02:12，已回滚后）

```
CPU        6.0%（/proc ticks 30 秒窗口）
/alive     max 149~170ms（偶发抖动，服务正常）
work_jobs  done 13075 / pending 1013 / unknown 17 / cancelled 2
已索引      27548
reconcile  RECONCILE_UNKNOWN_ENABLED = False（开关保留，代码保留）
```

**注意：回滚后 `unknown` 只有 17 条，不是原来的 1007——因为回滚前 reconcile 已经推回了一大批到 pending。**
**这些 pending 现在领不到（fence 闸门回来了），所以它们停在"推回去了但处理不了"的状态。**
**这不丢数据，只是那批记忆暂时仍无索引。**

## 保留下来的开关

`life/companion.py:179`
```python
RECONCILE_UNKNOWN_ENABLED = False
```
**这个开关是长期有用的：它控制"要不要主动回收 unknown"。**
**打开它之前，先满足上面的上线前提。**

## 相关的历史记录

- commit `e7dc340`（P70）：三处修复的原始实现，已被回滚但保留在 git 历史里
- commit `c17cee0`（P69）：索引扫描空转退避（5→60 秒），这个**已生效并保留**
- `ops/patches/20261119-0125-14-embedding-unknown-expires.py`
- `ops/patches/20261119-0130-15-memory-index-reconcile-unknown.py`
- `ops/patches/20261119-0132-16-claim-next-unknown-expires.py`
- `ops/patches/20261119-0131-17-wire-reconcile.py`
