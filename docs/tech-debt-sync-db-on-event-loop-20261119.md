# 技术债：事件循环里的同步数据库查询（2026-11-19）

## 症状

`/alive` 与 `/rpc chat` 在**同一个 aiohttp 事件循环**、同一个 `AppRunner`。
事件循环被同步 sqlite 查询占住时，两者一起卡。

实测 `/alive`（2026-11-19，每 100ms 采样）：

```
12:26~12:29   698 采样   13 次 >1s   最长 19.70s
12:36~12:39   874 采样    8 次 >1s   最长 16.95s   （修完 _projects 后）
12:43~12:46   331 采样   23 次 >1s   最长 5.03s    （索引 + 认知路径叠加时）
```

**用户能感知：她说话会卡几秒到十几秒。**

## 定量证据

### py-spy record 180 秒 / 4909 帧（MainThread）

```
37.7%  Repository._all            ← 同步 sqlite
24.5%  Repository._one            ← 同步 sqlite
 5.6%  JSONDecoder.raw_decode
 3.8%  EpollSelector.select       ← 真正空闲只有 3.8%
```

**同步 DB 合计 62.1%。**

### 一次 `life_recall.context()` 的 SQL 数量（她每条消息都走这条路）

```
耗时 402~519ms
SQL 总数 1208 条
```

按调用方聚合：

| 条数 | 调用链 |
|---|---|
| 154 | `epoch <- require_binding <- allowed` |
| 132 | `_allowed <- allowed <- load` |
| 127 | `parents <- _live <- live` |
| 120 | `get_event <- _native <- _live` |
| 120 | `_allowed <- allowed <- _dependencies` |
| 83 | `epoch <- require_binding <- load` |
| 81 | `search <- context` |
| 76 | `get_event <- allowed <- search` |
| 76 | `get_event <- _native <- body` |
| 64 | `resolve <- load <- allowed` |
| 61 | `resolve <- _live <- live` |

**最大单点是 `epoch <- require_binding`（154 + 83 = 237 次）。**

## 已定位的根因

### ① `require_binding` 每次调用都查一次全局版本号

```python
# life/life_access.py
def require_binding(self, ctx):
    if (... or ctx.policy_revision != self.catalog.epoch('policy')
            or self.binding_current and not self.binding_current(ctx)):
        raise RecallError('not_available')

# life/life_catalog.py
def epoch(self, domain):
    row = self.repo._one('SELECT epoch FROM life_epochs WHERE agent_id=? AND domain=?', ...)
    return row['epoch'] if row else 0
```

**`life_epochs` 是全局版本号，在一次 recall 内不会变。**
**但 `require_binding` 在 `allowed()` 和 `load()` 开头各调一次 → 一次 recall 237 条相同 SQL。**

**这是纯重复，不是语义需要。**（未修改，仅定位）

### ② `LifeCatalog.live` 递归（≈19.1%）

```python
def _live(self, resource, visiting, *, history, _cache):
    ...
    for edge in self.parents(ref, _cache=_cache):
        parent = self.resolve(edge['parent_id'], _cache=_cache)
        if not parent or ... or not self.live(parent, visiting | {ref}, history=history, _cache=_cache):
            return False
```

**有环检测（`visiting`）、有 memo（`_cache`）。**
**`_cache` 由 `LifeRecall.allowed` 创建（`cache = {} if _cache is None else _cache`），
在一次 search 内是共用的 —— 这一点没问题。**

**但每条候选资源仍然要展开各自的血缘，fan-in 图上共用祖先时能命中 memo，
不同祖先时无法避免新查询。**

**未定论：这 19.1% 是"血缘图本身的规模"还是"memo 没覆盖到的重复"。**

### ③ `_projects` 全表扫描（已修）

```
根因：`k LIKE 'action_task:%'`，SQLite 的 LIKE 默认区分大小写 → 走不了 k 上的索引
      → 每次全表扫 meta（19927 行），实测 120.44ms
修法：建 meta(k) 索引 + 改成 `k GLOB 'action_task:*'`
结果：120.44ms → 2.8ms（51 倍）
```

**但对 `/alive` 改善很小（19.70s → 16.95s），因为它只占 5.7%。**

**教训：热点排序必须是「频率 × 单次代价」，
不能是"调用点个数"（`_projects` 被 3 条链调用，合计只占 5.7%）。**

## 已知大头汇总

| 来源 | 占比 |
|---|---|
| `LifeCatalog.live` 递归 | ≈19.1% |
| `LifeRecall` 认知路径 | ≈10.7% |
| `MemoryIndex.scan_enqueue` | ≈2.8% |
| `_projects`（已修） | 5.7% → 接近 0 |

## 为什么现在不修

1. **`live()` 是目录解析，不是纯查询。** 改错会导致"看到已删除的记忆"或"看不到还在的记忆"
   —— 这是权限/可见性问题，比慢严重得多。
2. **索引正在跑（约 13 小时）。** 索引每轮让出事件循环一次，
   让出期间这些同步查询才有机会执行；停掉索引后 `/alive` 全部正常（实测 1180 次采样 0 次 >1s）。
   **所以现在的数字包含了"索引负载放大了既有问题"这一层。**
3. **需要先确定索引跑完后是否自愈。** `_scheduler` 仍然每秒一次，`_cognize` 每条消息一次，
   所以大概率不会完全自愈，但基数会低很多。

## 验收判据

```
/alive    p99 < 100ms，最长 < 1s
/rpc chat 端到端 < 5s（不含模型推理时间）
```

## 修复顺序建议

1. **`require_binding` 的 epoch 缓存**（收益最大、风险最低：全局版本号在单次 recall 内不变）
2. **`MemoryIndex.scan_enqueue`**（2.8%，可控）
3. **`LifeRecall` 认知路径**（10.7%，直接影响她说话）
4. **`LifeCatalog.live` 递归**（19.1%，风险最高，必须最后做且要有完整回归）

## 测量方法上的教训

**`py-spy dump` 拍的是"此刻"，不是"刚才"。**

我第一次用 `py-spy dump` 抓 5 次慢样本，栈全是 `EpollSelector.select (idle)`，
差点据此判断"不是事件循环阻塞"。**那是方法错**：`dump` 在 curl 返回后才执行，
阻塞窗口已经过去。

**抓偶发阻塞必须用 `py-spy record --rate 50` 连续采样**，或者"测到慢的那一刻立刻 dump"。

## 并行任务的干扰

做这组测量期间，机器上还有一个并行任务在跑 `snap install chromium`
（团队内 t40 的前置验证）。它占用 CPU 与 IO。

**规则：多 agent 并行时，做任何性能测量前先查机器上有没有别人的任务在跑。**
