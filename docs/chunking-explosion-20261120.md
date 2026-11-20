# 索引切块爆炸：一个 bug 解释三个现象

取证：2026-11-20 00:39–01:05　HEAD `7e665ea`
**只读代码与数据库，未改任何文件。**

---

## 0. 一句话

`life/memory_index.py` 的 `chunks()` 在一类文本结构下每轮只推进 1~2 个字符，
导致块数膨胀 20 倍以上。**v2 索引里 68.9% 的片由 6.3% 的资源产生**，
这同时解释了「索引膨胀」「`scan_limit` 失效」「检索结果同质」三个看起来独立的现象。

---

## 1. bug 本体

`life/memory_index.py:47-74`

```python
while end < len(text) and estimate_tokens(text[start:end+1]) <= config.chunk_tokens:
    end += 1
    if text[end-1] in '\n。！？!?；;':
        last_boundary = end          # 窗口内**最近一次**见到的边界
if end < len(text) and last_boundary and last_boundary > start:
    end = last_boundary              # ← 把 end 拉回那个边界
...
overlap_start = end                  # ← 从这个（被拉回的）end 往回退
while overlap_start > start+1 and estimate_tokens(text[overlap_start-1:end]) <= config.overlap_tokens:
    overlap_start -= 1
start = max(start+1, overlap_start)  # ← start 几乎没前进
```

**缺陷：`last_boundary` 只被约束为 `> start`，没有被约束为 `> 上一轮的 end`。**

当窗口内最后一个边界落在很靠前的位置时，`end` 被拉回到那里、块极短、
重叠回退又几乎退不动 → `start` 每轮只前进 1~2 字符 → 下一轮窗口几乎没变，
`last_boundary` 还是同一个点 → 死循环式推进。

### 逐 step 实证（真实文本 `evt_623d03763d1f6a2c64ebe4ff`，10651 字符）

```
step 0: start=0    扫描到 end=443  last_boundary=309 → end=309  len=309  推进 277
step 1: start=277  扫描到 end=676  last_boundary=647 → end=647  len=370  推进 338
step 2: start=615  扫描到 end=1114 last_boundary=685 → end=685  len=70   推进 38
step 3: start=653  扫描到 end=1202 last_boundary=685 → end=685  len=32   推进 1  ←
step 4: start=654  扫描到 end=1203 last_boundary=685 → end=685  len=31   推进 1  ←
step 5: start=655  扫描到 end=1204 last_boundary=685 → end=685  len=30   推进 1  ←
```

**该文本的块 #2 区间 `[1019, 1229)` 内一个边界字符都没有，但 `last_boundary` 仍指向 685。**

---

## 2. 最小复现（不依赖生产数据）

```python
cfg = IndexConfig()   # chunk_tokens=384, overlap_tokens=48

A '这是一段没有任何标点符号的很长的文本' * 50                     chars=900   ×1.0  正常
B '。'.join(['短句'] * 500)                                    chars=1499  ×1.0  正常
C '。'.join(['短句']*20) + '这'*6000 + '。'.join(['尾巴']*20)    chars=6118  ×2.2
D ('这是一个稍长一点的句子大概三十个字左右的内容。' + '无标点长片段'*200) * 3
                                                               chars=3669  ×6.6  ← 爆炸
```

**触发条件：长短交替**——文本中既有密集句号，又有超过 `chunk_tokens` 的无边界长片段。
全无边界（A）和全短句（B）都正常，解释了为什么大部分资源没受影响。

---

## 3. 影响面（v2，`state='ready'`）

| 块数档 | 资源数 | 片数 |
|---|---|---|
| 1 | 6913 | 6913 |
| 2-3 | 1054 | 2324 |
| 4-10 | 346 | 2163 |
| 11-50 | 413 | 11031 |
| **51-200** | **580** | **46424** |
| >200 | 11 | 3384 |
| 合计 | 9317 | 72239 |

```
>50 块的资源：591 条，49,808 片 = **68.9%** 的 v2 索引片
```

**v1 几乎不受影响**（13093 条中只有 1 条 >50 块）——因为 v1 产出的纯空白片被过滤后
`piece.strip()` 的判断改变了推进节奏。

---

## 4. 内容完整性：**没有丢失**

8 条最长资源的区间并集与原文对比：

```
resource                        n     覆盖     原文字节   越界块  空洞
life_ce85d913…                463   101.1%    14189      2      0
life_7c691fed…                462   101.1%    14100      2      0
life_a3c623d3…                374   102.7%     5728      1      0
life_030c9893…                335   101.2%    13529      1      0
life_a24cd6a3…                331   101.1%    13440      2      0
life_4d8ab9ea…                322   101.1%    13593      2      0
life_5e4cbc7c…                249   100.6%    25270      2      0
life_42f363cd…                218   103.8%     4000      1      0
```

**空洞全部为 0 → 全文都被覆盖，没有内容丢失；覆盖率 >100% 是重叠造成的。**
**所以这是「切得碎」，不是「内容残缺」。**

### 但每条都有 1~2 个越界块

```
原文 14189 字节，却存在 [13798, 14277) 和 [14213, 14350) 两个块
→ byte_end 超出原文长度 88 / 161 字节
```

**这些越界块的 `chunker_version` 长度 41（正常）、`vector` 长度 2048（正常）、`state='ready'`**
——**它们被当作有效索引行存下来了。**

**全库检查：**
```
vector 字段：100611 行全部是 blob，长度全部 2048（=512 维 float32），
全零向量 0 行。
```

**结论：没有空向量。越界块存的是空/近空文本算出的正常向量，
它们会参与相似度计算，但对检索的实际影响未测。**

---

## 5. 两个同名 `estimate_tokens`（潜在的静默错误源）

```python
# life/resource_policy.py:80
def estimate_tokens(value):
    return max(1, math.ceil(len(body.encode('utf-8')) / 2))     # 字节数 / 2

# life/text.py:42
def estimate_tokens(text: str):
    han = len(_HAN.findall(s)); other = len(s) - han
    return max(1, han + other // 4)                             # 汉字 + 其他/4
```

同一段 10651 字符文本：**7095 vs 3901，比值 1.82**。

`memory_index` 导入的是 `resource_policy` 那个（实测 `mi.estimate_tokens is est_rp → True`）。

**两者都不是 bug（`resource_policy` 版注释写明 "Conservative UTF-8 estimate"），
但同名两实现是隐患：任何一处 import 改动都会让切块结果完全不同，
而 `chunker_version` 察觉不到。**

---

## 6. `chunker_version` 只对参数做指纹（漏洞及具体形态）

```python
# life/memory_index.py:87-88
self.chunker_version = 'utf8-paragraph-v2-drop-blank:' + sha(encode(
    [self.config.chunk_tokens, self.config.overlap_tokens]))[:12]
```

**指纹只覆盖 `chunk_tokens` 和 `overlap_tokens` 两个参数，不覆盖：**
```
① chunks() 的实现逻辑
② estimate_tokens 的实现（来自哪个模块）
③ 任何影响输出但不在参数里的东西
```

**具体后果：改了 `chunks()` 逻辑或 `estimate_tokens` 的 import，索引不会失效，
新旧块会混在库里，而版本号相同。**

**修法建议（二选一）：**
```python
# 方案 A：把实现也纳入指纹
import inspect
est_src = inspect.getsource(memory_index.estimate_tokens)
self.chunker_version = 'utf8-paragraph-v3:' + sha(encode([
    config.chunk_tokens, config.overlap_tokens, est_src]))[:12]

# 方案 B（更简单）：显式断言来源
from .resource_policy import estimate_tokens as _est
assert _est.__module__ == 'life.resource_policy'
```
**方案 A 更彻底——版本号要覆盖「会影响输出的一切」，不只是参数。**

---

## 7. 未解释：`stored ≠ now`

```
库里的 stored 块数        463
当前代码重跑              440
换成 text 版 estimate     364
```

**排除了三个假设：**
```
✗ estimate_tokens 变过 —— git log -L 显示从 baseline 起没改过
✗ 原文变过 —— source_version 全部为 1，且每条资源在索引里只有一种 version
✗ chunker_version 变过 —— 全部是 v2 / 5a62b6071bf4
```

**最可能的解释（未验证）：** `events.payload` 里的 `text` 在入库后可能被清洗过
（去控制字符等），而 `source_version` 只做语义层面的版本判断、不检测字节级变化。

**这是一个脆弱点，但没有证据，不作为结论。**

---

## 8. 索引速率与重建成本

```
work_jobs（kind='memory_index'）：
  done 23229 / pending 8262 / running 5 / cancelled 2 / unknown 5

速率：
  整体平均（479.7 小时跨度）  0.81 job/分   ← 说明任务是间歇性跑的
  最近 30 分钟               12.13 job/分
  最近 60 分钟               14.53 job/分

全量重建 16922 条的估算：
  按 12.13/分 → 23.2 小时
  按 14.53/分 → 19.4 小时
  （前提是索引任务持续满速；整体平均 0.81/分 说明实际会被其它任务挤占）
```

**队列里当前排的是 v2 任务：**
```
done 10151 / pending 7579 / running 1 / unknown 5
```

---

## 9. 切 v3 之后 pending 的 v2 任务会怎样

`memory_index._unindexed()` 的过滤条件（`memory_index.py:108-117`）：

```sql
AND NOT EXISTS (SELECT 1 FROM life_semantic_index i
  WHERE i.resource_id=r.resource_id AND i.source_version=r.source_version
    AND i.model_key=? AND i.chunker_version=? AND i.state='ready')
```

**按 `chunker_version` 过滤 → 切到 v3 后，v2 的索引行不再满足条件，
资源会重新进入 `_unindexed`。**

**但队列里已排队的 7579 个 v2 任务不会自动变成 v3 任务——它们的 payload 里
带的是 v2 的 `chunker_version`。**

**所以「切 v3」不是一步干净的操作，需要同时处理：**
```
① bump chunker_version 到 v3
② 清掉或作废队列里的 v2 job（否则它们跑完也是白跑）
③ 让 _unindexed 把全部资源重新入队为 v3
```

---

## 10. 建议

```
① 先修 chunks()（缺陷在 §1，`last_boundary` 需要同时满足 > start 和 >= 上一轮的 end）
② 用 §2 的 D 用例做回归（必须从 ×6.6 降到 ~×1.0；A/B 用例保持 ×1.0）
③ 用真实文本验证：块数、内容完整性（空洞=0）、越界块=0
④ chunker_version 升 v3，并把 estimate_tokens 的实现纳入指纹（§6 方案 A）
⑤ 清掉队列里的 v2 job，让全部资源重新入队为 v3
⑥ 全量重建（预估 19~23 小时满速；实际更长，因为索引任务是间歇性跑的）

不重建的理由只有一个：现有 v2 有 68.9% 的片是坏的，修了代码不重建等于没修。
```

---

## 11. 取证命令

```bash
git -C /opt/qq-deepseek rev-parse --short HEAD          # 7e665ea
grep -n "def chunks" life/memory_index.py
grep -rn "def estimate_tokens" life/
git log -L :estimate_tokens:life/resource_policy.py
git log --format="%h %ad %s" --date=format:"%m-%d %H:%M" -- life/memory_index.py
```
