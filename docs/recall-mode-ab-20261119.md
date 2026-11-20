# 语义召回卡顿排查与 A/B（2026-11-19）

## 一、问题

下午 17:00 前后，服务出现：
- `journalctl` 每 30~40 秒一条 `语义召回超时，本轮只用词法/结构路线`（60 分钟内 99 次）
- `/alive` 有多次 5 秒以上超时
- CPU 一度被观察到 105%

**但这个问题在我介入复测时不能稳定复现**（CPU 只测到 0.13~0.31 核），
所以后面的一切测量都要先确认"症状在不在"。

## 二、根因链

### ① `search_vectors` 遍历全部允许资源

`LifeRecall.search()` 在 `mode='auto'` 下走三条路线：

```python
routes = [text_route, semantic, related] if mode == 'auto' else ...
```

其中 `semantic` 来自 `MemoryIndex.search_vectors(allowed, query_vector)`
（`life/memory_index.py:344`），它对 `allowed` 里的**每一个**资源逐条扫索引行：

```python
for resource in allowed:
    rows = self.repo._all("SELECT * FROM life_semantic_index WHERE resource_id=? ...",
                          (resource['resource_id'], ...))
```

实测 `allowed` 有 **9314 个资源**，v2 索引 36270 行。

各 mode 耗时（budget=8000，同参数只改 mode）：

| mode | 耗时 |
|---|---|
| text | 455 / 444 ms |
| semantic | 3305 / 9331 ms |
| related | 1474 / 1576 ms |
| auto | 7353 / 6189 ms |

**`find_related` 单测只有 10~11ms（我一度怀疑它，不成立）；
真正的开销是 `search_vectors`。**

### ② 5 处 `context()` 里 4 处的调用者是同步函数

```
life/companion.py:2089              → Companion._messages          （def，同步）
life/companion_recollection.py:34   → Recollection.pick           （def，同步）
life/companion_town.py:565          → Companion._handle_turn      （async def）
life/continuity.py:482              → Continuity.context          （def，同步）
life/runtime.py:611                 → context_block               （def，同步）
```

`_messages` 里共有 **2 次同步 `context()`**（2089 行用当前 query、2115 行经 `pick()` 用她自己的项目目标），
而 `pick()` 的 cues 来自 `origin=='self' && status=='active'` 的项目 goal，
**不是重复查询，不能去掉**。

`LifeRecall.context()` 本身是纯同步函数（`life/life_recall.py:368`，无 await），
它被 `Companion._messages` 调，而 `_messages` 在 `_workers()` 这个 asyncio 任务里被 `await`——
**所以它直接阻塞事件循环，`/alive` 排在同一循环里就会等。**

### ③ `require_binding` 每次都查全局 epoch

`LifeAccess.require_binding()`（`life/life_access.py:70`）：

```python
or ctx.policy_revision != self.catalog.epoch('policy')
```

`LifeCatalog.epoch()` 每次都 `SELECT epoch FROM life_epochs WHERE ...`。
一次 `mode=auto` 的 recall 实测发出 **48253 条 SQL**，其中：

```
10322  epoch <- require_binding <- allowed/load   （纯重复，全局版本号一次内不变）
 5798  _allowed <- allowed <- load
 4504  parents <- _live <- live
```

## 三、实验：三组对照

**观测脚本同一个**：每 60 秒一个窗口，窗口内每 200ms 打一次 `/alive`，
记录 max、`>1s` 次数、CPU（`/proc/<pid>/stat` 两次差值）。

| 组 | 配置 | 重启 | 峰值中位 | 最差 | >1s 次数 | CPU 平均 / 最大 |
|---|---|---|---|---|---|---|
| baseline | auto | 否 | 3812ms | 5026ms | 27 | 0.13 / 0.22 核 |
| text-override | text | 是 | **1022ms** | 8551ms | **11** | 0.16 / 0.35 核 |
| auto-restart | auto | 是 | 4772ms | 28111ms | 40 | 0.31 / 0.67 核 |

**混杂变量排除**：实验组和对照组 A 都重启过，唯一差别是 `mode`，
而 `>1s` 次数差 3.6 倍、最差值差 3.3 倍 —— **不是重启的功劳。**

此外 `auto-restart` 组比 `baseline` 还差（40 vs 27 次），
说明 auto 本身不稳定，重启不能改善它。

**text 组的逐分钟明细**：

```
min1  8551ms(>1s:4)   ← 首启噪声
min2  1391ms(>1s:1)
min3   291ms(>1s:0)
min4~9  1518/1886/1181/1149/1189/1022ms (>1s:1 各一次)
min10~15  898/744/750/816/755/770ms (>1s:0 全部)
```

## 四、结论

**`RECALL_MODE_OVERRIDE=text` 使 `/alive` 峰值中位从 3812ms 降到 1022ms（-73%），
`>1s` 次数从 27 降到 11（-59%），稳定段 750~898ms。**

实现方式（`life/life_recall.py`）：

```python
def search(self, ctx, query='', *, mode=None, ...):
    if mode is None:
        mode = os.environ.get('RECALL_MODE_OVERRIDE') or 'auto'
```

注入方式（不动机 unit 文件）：

```
/etc/systemd/system/companion.service.d/recall-mode.conf
[Service]
Environment=RECALL_MODE_OVERRIDE=text
```

**回滚零成本：删掉 drop-in 文件并重启即可，行为恢复成 `auto`。**

## 五、代价

**text 模式下不走向量语义检索，只剩词法（`life_terms`）与结构路线。**

**当前状态：**
- 60 分钟内观测到 99 次 `语义召回超时`，同期 `inference` 事件 100 个，比例约 99%
- **但没有证据表明 auto 模式下有没有成功过的轮次** —— 成功时不打日志，无法从"没有成功日志"推出"没有成功轮次"

**因此这一条有两种可能，我无法判定：**
- 如果 auto 偶有成功，那 text 是**有损**的（少了那些成功的语义命中）
- 如果 auto 从来没成功，那 text 是**无损**的（只是把已经发生的降级提前了）

**验证方式：需要把 `_recall_status` 落库（现在只在 `self._last_material_extra['recall_route']` 里，不持久化），
才能统计成功/失败比。这一步没做。**

## 六、未定论

1. **14 秒 / 28 秒的极端值未解释。** py-spy record 对齐后，
   三个 >2 秒的慢窗口里 `LifeRecall.context/search` 都是最大单项，
   但其中一个 14.4 秒的窗口只有 56% 采样落在 recall 里，其余是 idle 和 JSON 解码。
   多轮重叠假设已证伪（103 个 `state_time` 事件间隔 min 20.15s，`<2s` 占比 0/102）。

2. **`dialogue.seconds` 不是纯生成时间。** 它是
   `companion_model.complete()` 的墙钟范围（`life/companion_model.py:83` 起、
   `:212` 止），含服务商解析、网络往返、服务端排队、重试。

3. **auto 模式下语义召回的成功率未知**（见第五节）。

## 七、教训

**今天识别出五次会让结论失效的混杂变量：**

1. 重启副作用（实验组重启了、基线没重启 → 补做 auto+重启对照组）
2. budget 混淆（`context(budget=800)` vs `search_async(budget=8000)` → 不可比）
3. diagnostics 开关（`diagnostics=True` 会额外计算 `match_routes`）
4. 并行任务（`snap install chromium` 在测性能期间占用 CPU/IO）
5. 两次不同条件的数据对比（12:57 的 20 秒 vs 切 text 后的 37 秒——她当时做的事不同）

**以及两次说"止血"又撤回：**
- `/alive` 好了 ≠ 她不卡了
- 她说话慢 ≠ 系统卡（37 秒里 32 秒是她在读 B 站字幕、做活动）

**判据：说"好了"之前，先问"好在哪个指标上、坏在哪个指标上"。**

## 八、指标定义表

**这一节比前面七节更有用，因为教训是叙事，表是工具。**

| 指标 | 它实际测的是什么 | 它不能回答什么 | 验证方式 / 正确定义 |
|---|---|---|---|
| `/alive` 响应时间 | 事件循环健康度（含排队） | 用户感受的"她多久回我"、系统总耗时 | 同窗口配合 py-spy record 对齐 |
| `chat` 端到端 | 她回一句话的全部时间（含她同时在做的 `activity` / `bilibili_read` / `town`） | 她"说话"的耗时 | 看 `usage` 里 `purpose='dialogue'` 的 seconds |
| `dialogue.seconds` | 单次模型调用的**墙钟**时间（含网络 + 服务端排队 + 重试） | 纯生成速度 | 与 `output_tokens` 线性 R²=0.78，可作趋势；不可作生成速度 |
| `RSS` 之和 | 各进程自报的驻留集（共享页重复计） | 这台机器实际少了多少内存 | `cgroup memory.current` 或 `available` |
| `ps %CPU` | 进程生命周期均值 | 瞬时占用 | `/proc/<pid>/stat` 的 utime+stime 两次差值 ÷ 窗口秒数 |
| `py-spy dump` | **此刻**的栈（attach 时暂停一拍） | "刚才在干嘛" | 抓偶发阻塞用 `record --rate 50` 连续采样 |
| `LIKE '%x%'` / `LIMIT N` | 符合条件的前 N 条 | 总量 | 先 `COUNT(*)` 再看具体行 |
| `ls` 失败（`cannot access`） | 这个路径下不存在 | 东西不在 | 查别处（真实路径可能不同） |
| `json_extract(usage,'$.prompt_tokens')` | — | — | 实际字段是 `input_tokens` / `output_tokens`，先 `SELECT usage` 看结构 |
| `EXPLAIN QUERY PLAN` + `LIKE 'prefix%'` | 查询计划 | 会不会用到索引 | SQLite 的 LIKE 默认区分大小写，无法走索引；用 `GLOB` 或范围比较 |
| 代码里的 `if x IS NULL` | 静态阅读的推断 | 运行时实际行为 | 直接调那个函数，看它返不返回 |

## 九、最终结论与技术债

### 9.1 今天解决了什么

| # | 结论 | 证据 |
|---|---|---|
| 1 | `/alive` 卡顿缓解：`RECALL_MODE_OVERRIDE=text` 使峰值中位 3812ms → 1022ms（-73%），`>1s` 次数 27 → 11（-59%） | 三组各 15 分钟对照，观测脚本相同、实验组与对照组 A 都重启过（混杂变量已排除）；`auto-restart` 组 4772ms/40 次反而比基线差 |
| 2 | `_prefetch_recall` 的语义补充只在 dialogue 轮发起 | `purpose` 分布实测 activity 2045 / work 1053 / dialogue 830 / town 608 / bilibili_read 192 / executor 70，即 dialogue 只占 ~17%；加守卫后最近 10 分钟 journal 的 `语义召回超时` 与 WARNING/ERROR 均为 **0 条** |
| 3 | 15 分钟的空闲空档不是故障 | `_idle_seconds()` 的 `idle_base_seconds` 默认 900 秒；实测 6 段空档中 5 段 ≈ 900 秒（883/923/951/924），1 段 1335 秒且有 `Stopping/Stopped/Started`（人工重启） |

### 9.2 今天想解决但没解决

| # | 未解决的事 | 原因 |
|---|---|---|
| 1 | 语义召回当前不可用 | `mode=auto` 的 `search()` 实测 6~9 秒，而 `_prefetch_recall` 的 timeout 是 0.8 秒（原 2.0），必然超时 |
| 2 | `allowed()` 在 auto 下要 3.6 秒 | 它对 15759 个资源逐个做 ACL + event 查询；一次 auto search 实测 53354 条 SQL（其中 `life_epochs` 12433 条是纯重复、`life_semantic_index` 2250 条、`life_acl` 9548 条查的是空表） |
| 3 | 优化到极限仍不达标 | 空表短路 + epoch 缓存合计约省 35~45%，5886ms → 约 3.2~3.8 秒，仍 > 2 秒；**这条路走不通**，需要换设计 |
| 4 | text 模式下 `_prefetch_recall` 的 timeout 只降到 0.8 秒、未做架构改造 | 「异步化/后台化」会撞上 `Repository` 的单连接 + `RLock` 串行（`check_same_thread=False` + 全局 `threading.RLock`），总时间不变，只是把等待从事件循环移到后台线程 |

### 9.3 未验证项

| # | 未验证的事 | 为什么重要 |
|---|---|---|
| 1 | **dialogue 轮次是否仍会超时** | 加守卫后，非 dialogue 轮不再发起；但 dialogue 轮仍走 `search(mode=auto)` → 6~9 秒 > 0.8 秒，大概率仍超时。今天下午之后没有用户对话，**没有观测样本** |
| 2 | 早前"60 分钟 99 次超时"期间 auto 有没有成功过的轮次 | 成功时不打日志，无法从"没有成功日志"推出"没有成功轮次"；要 `_recall_status` 落库才能统计 |

### 9.4 技术债（今天发现、不在今天范围）

| # | 债 | 收益/风险 | 改动大小 |
|---|---|---|---|
| 1 | `allowed()` 在 auto 下逐资源做 ACL + event（53354 条 SQL/次） | 真解法是 ANN / 缩小 allowed 集合 / 后台化，收益大 | **大**（需要重新设计检索路径） |
| 2 | `life_acl` 是空表，但 `_allowed` 仍对每个资源查 2 次（9548 条/次 auto） | 收益约 5%，**风险是权限语义**：`_allowed` 后面还有三条早退（`mode` / `origin_scope == audience_key` / `same_person_group_event`），空表短路会跳过后两条 → 可能看到跨 scope 资源 | 小，但**不建议做**（权限函数不许为性能精简分支） |
| 3 | `require_binding` → `epoch('policy')` 重复 12433 次/次 auto | 纯重复（全局版本号一次内不变），省 12~15% | **小**（per-search 缓存即可），但只在 auto 下有收益，而 auto 已不用 |
| 4 | `search_vectors` 的 `scan_limit=10000` 会在超限时 `return [], 'partial'` | 隔离测试实测：`allowed` 9453 资源 × 4.98 片 = 47101 片 > 10000，会整批返回空。**但生产里 `search_vectors` 从未被调用**（探针 5 分钟 0 条记录，因为 `query_vector` 只在 mode ∈ {auto, semantic} 时才计算，而现在全是 text 或超时）→ **未触发**，属潜在问题 | 小（改 `break` 而非 `return []`） |

## 十、被推翻的判断（留档，防止后人重走）

| 判断 | 状态 | 推翻方式 |
|---|---|---|
| `index_access_fence IS NULL` 造成死锁 | **推翻** | 实际调 `claim_next` 5/5 都能拿到 lease |
| N+1 查询是 recall 慢的主因 | **推翻** | 写了批量版（`IN (...)` 每批 500），实测比 N+1 **更慢**（0.7x） |
| 计算占 69% | **推翻** | 那是被 `scan_limit` 截断后的隔离测试，两种实现都扫到 10001 片就退出，根本没跑完 |
| `scan_limit` 导致语义召回返回 0 条 | **撤回** | 生产探针证明 `search_vectors` 从未被调用（`query_vector` 在 text 模式下不计算） |
| `query_vector()` 慢导致超时 | **推翻** | 实测 `adapter.embed` 中位 184ms、`query_vector` 中位 1ms（缓存命中）、8231 直连 60ms |
| `allowed()` 被调 3 次 | **推翻** | 实测一次 search 只调 1 次，但它自己占 62%（3632ms） |
| 空表 `life_acl` 可以短路成"全部允许" | **推翻（我提的，被自己查证拦下）** | `_allowed` 后面还有三条早退判断，短路会改变权限语义 |

## 附：本次改动的文件

```
life/life_recall.py   search() 的 mode 参数可被 RECALL_MODE_OVERRIDE 覆盖
                      （不设变量时行为与原来完全一致）

life/companion.py     _prefetch_recall 的 timeout: 2.0 → 0.8
                      _prefetch_recall(..., purpose=None)：非 dialogue 轮直接跳过
```

**数据库索引（一次性，可回滚）：**

```sql
CREATE INDEX meta_k ON meta(k);   -- 157ms，+1MB，DROP INDEX meta_k 可回滚
```

**其它今天已经完成、与本问题无关的改动见 `docs/memory-index-six-layers-20261119.md`。**
