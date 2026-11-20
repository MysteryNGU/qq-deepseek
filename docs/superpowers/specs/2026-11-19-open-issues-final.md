# 未解决问题清单（2026-11-19 收工时）

日期：2026-11-19 23:05（北京时间）
基线：云端 `47.104.8.84:/opt/qq-deepseek`，HEAD `5f62038`，工作区干净（除 `2500`/`skills/` 未跟踪）
用途：明天（或交给别人）继续时的入口。每条都写明**现状、证据、为什么没做、怎么验证**。

---

## 0. 当前生产状态

```
服务      active / NRestarts=0
/alive    2.82s（这一次；中位通常 1.3~8ms，偶有 5~20 秒峰值，见 P3）
HEAD      5f62038，工作区干净
磁盘      49G 总，16G 用，32G 可用
env       RECALL_MODE_OVERRIDE=text   ← 今天的临时措施，仍生效
索引      done 21836 / pending 9342 / running 5 / unknown 5
          v1 ready 27630 / v2 ready 64501
```

**注：`/alive` 这次 2.82s 是慢样本，不代表常态。**

---

## P0：找记忆这条路从来没工作过 —— **最高优先**

### 现状

```
context(budget=800) 必然抛 RecallError('budget_requires_page')
→ 被 `except RecallError: return ''` 静默吞掉
→ compact_recall('') 返回 []
→ 那一段召回候选恒为空
```

**实测骨架：空结果的 JSON 骨架 = 1154 token（绝大部分是 `next_cursor` 的 base64 签名游标）。**

**`budget < 1162` 时，无论有没有命中，必然失败。**

```
budget=200~1000  → budget_requires_page
budget=1200      → OK  count=0  estimated_tokens=1162
budget=1600      → OK  count=1
budget=2400      → OK  count=3
budget=4000      → OK  count=2
```

### 受影响的调用点（3 处硬编码 800）

```
life/companion.py:2089              _messages 拼候选        budget=800  → 必然空
life/companion_town.py:565          小镇上下文              budget=800  → 必然空
life/companion_recollection.py:34   pick 一条回忆           budget=800  → 必然空
life/continuity.py:482              连续输入的历史          budget=max(0, total-used)  → 视情况
life/runtime.py:611                 通用 context_block      budget=max(0, budget-estimate)  → 视情况
```

### 为什么没做

**这是改变行为，不属于当天"固定现状"的范围（当天在做 golden 契约）。**

### 修法方向（待设计确认）

```
A. 给 search() 加 include_cursor=False（context 用）
   → 骨架从 1154 token 降到几十 token，改动小、语义清楚
   → 前提：先确认 5 处调用方各自需不需要翻页
      已知：_messages / pick / town 不需要
      待查：continuity / runtime 两处
B. context() 里传 include_cursor=False 并重算预算
C. 单独给 context 一个"只返回文本"的轻量路径
```

### 验证方式

```
① 修完后，context(budget=800) 不再抛 budget_requires_page
② 骨架 token 从 1154 降到 < 200
③ 5 处调用点各自跑一次，确认返回内容非空（有命中时）
④ 用 golden（tests/golden_recall_20261119.json）对比 text 路线结果不变
```

### 为什么严重

```
不是"召回效果差"，是"召回这一段从来没产出过内容"。
用户感受是"她记不住"，实际是"找记忆的那一步，预算不够，什么都没拿到，
而且系统没告诉她"。
```

---

## P1：`scan_limit=10000` 让语义路线恒返回 0 —— **语义召回的第二个成因**

### 现状

```
allowed 资源    9874
v2 索引片数     64501（还在涨）
平均            6.4 片/资源
scan_limit      10000

search_vectors 遍历 allowed，累计 scanned > 10000 就：
    return [], 'partial'      ← 整批返回空，不是"返回已扫到的部分"
```

### 证据（今天实测，非推测）

```
'oracle 副本上，ignore_override=True 真跑 semantic：
  '热液'         semantic  count=1  eff_mode='semantic'  routes={'text':1}  28553ms
  '今天做了什么'  semantic  count=3  eff_mode='semantic'  routes={'text':3}  28918ms

→ eff_mode 正确，但 match_routes 里只有 text
→ semantic 跑了 28 秒，贡献 0 条
```

### 为什么没做

**这是"改变行为"，和 P0 的 golden 契约冲突，要分开做。**

### 修法方向

```
A. 调大 scan_limit（10000 → 70000）
   代价：实测扫 1 万片要 28.5 秒，扫 7 万片约 200 秒？需实测
B. 改截断行为：break 而不是 return []，保留已扫到的部分
   → 结果不全但至少不是 0
C. 缩小候选集（设计文档 §3.1 有方向：先排序候选再精确授权）
   → 真解法，但工作量大
```

### 验证方式

```
① 真跑 semantic，match_routes 里出现 'semantic'
② 返回条数 > 0
③ 耗时在可接受范围内
```

---

## P2：`mode=text` 时 `search_vectors` 根本不调用 —— **语义召回的第三个成因（当前生效中）**

### 现状

```
env RECALL_MODE_OVERRIDE=text（今天上午为了缓解 /alive 卡顿加的）
→ 所有路径都变成 text
→ query_vector 不算、search_vectors 不调
→ 语义召回完全不用
```

### 为什么这样

**今天上午 `/alive` 卡到 28 秒，根因是 `allowed()` 在 auto 模式下要 3.6 秒（53354 条 SQL）。**
**临时把 mode 改 text，`/alive` 峰值中位从 3812ms 降到 1022ms。**

### 撤掉的前提

```
P3（同步 DB 结构问题）修完，或召回管线重构（P4）上线后
→ 才能把 RECALL_MODE_OVERRIDE 撤掉
```

**判据：撤掉后 `/alive` p95 仍 ≤ 100ms。**

---

## P3：事件循环里做同步 DB，主线程 55.7% 在同步 sqlite

### 现状

```
py-spy 45 秒，2523 个样本（MainThread）：
  37.9%  Repository._all          ← 同步 DB
  17.8%  Repository._one          ← 同步 DB
   7.7%  JSONDecoder.raw_decode
   4.2%  JSONEncoder.iterencode
   2.3%  EpollSelector.select     ← 真正空闲只有 2.3%

Repository = 单连接 + threading.RLock，所有查询串行
/alive 和 /rpc chat 在同一个事件循环
```

### 为什么没做

**这是结构性改动，设计文档 §4 给了方向（RecallReadSession + 专用只读 worker），
属于召回管线重构的一部分，不在今天范围。**

### 修法方向（设计文档 §4）

```
RecallReadSession：
  · 专用 worker 中创建只读 SQLite 连接，query_only=1
  · 确认 WAL 模式后使用短读事务
  · 只读 session 及其 catalog/access 依赖必须绑定该连接
  · 不在跨线程传递 sqlite Row / 活动游标 / 事务

首版：一个专用召回 worker，最多一个运行请求 + 一个最新待处理请求
```

### 验证方式

```
① /alive p95 ≤ 100ms
② 主线程空闲率从 2.3% 提到 > 30%
③ 同步 DB 占比从 55.7% 降下来
```

---

## P4：召回管线重构（设计 §3 的核心）—— **最大的一块，还没开始**

### 现状

**当前 `allowed()` 先遍历 15759 个资源做 ACL，`search_vectors` 又逐资源读向量。
结果预算只作用在末端。这是两层 N+1 和过早加载正文。**

**设计文档 §3.1 的方向：**
```
规范化请求 → 各路线生成排序候选 ID → 合并去重
→ 对候选做完整权限/来源资格验证 → 只加载通过验证的正文 → 组装预算内结果
```

### 已完成的准备工作

```
✓ P0 golden 契约（tests/golden_recall_20261119.json，标 INCONCLUSIVE）
  · 96 组合，非空 45.8%（< 80% 阈值）
  · errors: budget_requires_page × 44
  · routes 只有 text，semantic/related 贡献 0
  · 自带 generated_under + 6 条 known_limitations + rebuild_copy.sh
```

### 为什么没做

**这是设计里最大的一块，需要 A→B→C 分期，而且要新旧并存验证。**

### 实施顺序（capability-engineer 给的清单，已调整 P0 前置）

```
P0  golden 契约（已完成，标 INCONCLUSIVE）
P1  RecallReadSession（只读 worker + query_only=1）
    → 单独部署，验证不影响 /alive
P2  rank_candidates + effects_of（只出 ID 和分数）
    → 每写完一个，用 golden 验证这一个的排序
P3  search_v2() 组装
P4  完整差分（新旧全字段对比，逐条顺序全一致）
P5  切换（只有 P4 通过）
P6  观察 /alive p95 与召回 p95
```

**开关：`RECALL_PIPELINE=legacy|v2`，默认 legacy。**

---

## P5：`strict_schema` 把 27 个可选字段变必填 —— 1.94% 认知轮次完全失败

### 现状

```
strict=True（OpenAI strict function calling 要求，不能改）
→ strict_schema 给所有 object 强制 additionalProperties=false + required=全部 properties

实测影响：8 个 object 节点共 27 个"原本可选"的字段被变必填
顶层 required 从 1 个（reply）变成 21 个

24 小时：13 次最终失败 / 670 轮 = 1.94%
```

### 三种失败形态

```
A. 多写键（additionalProperties 拒绝）    1 次  drive_feedback[0] 多 'project': ''
B. 缺必填（required 拒绝）                4 次  全部是漏了 'reply'
C. 类型错（followups）                    1 次
```

**注意：`reply` 是唯一**原本就必填**的字段，而它恰好是被漏掉的那个。
→ 必填字段越多，模型越容易漏掉真正重要的那个。**

### 为什么没做

**这是改 schema 结构，影响面大，需要设计决策。**

### 修法候选

```
D. 拆成两次模型调用（代价大）
E. description 里写明哪些字段不要写（治标）
F. 白名单式清理已知多余键（最小，但每遇到一个新键加一条）
G. 接受当前 1.94%（已重试一次，重试救回约 50%）
```

**建议：先积累"多余键"和"漏填键"的样本，有 3~4 种稳定模式后再按 F 加白名单。**

### 需要设计决定的

```
重构 schema 结构（比如拆成两次调用）？
如果要重构，注意：别把"减少必填字段"变成"减少表达能力"。
```

---

## P6：7/13 次失败无法追查 —— 日志轮转

### 现状

```
24 小时最终失败 13 次
能在 journal 里找到具体校验位置的：6 次
找不到的：7 次（journal 已轮转，只剩 "本轮返回结构无效" 一行）
```

### 为什么没做

**数据已经没了，追不了。**

### 建议

```
① 这类"结构无效"的失败，把 raw content 存到 artifact（不只写日志）
② 现在的错误消息是 "本轮返回结构无效，原始消息和实际回执已保留"
   —— 要去确认"已保留"到底保留到哪，能保留多久
```

---

## P7：`life_acl` 是空表，但每个资源查 2 次

### 现状

```
life_acl 表：0 行
auto 模式下 _allowed() 对每个资源查 2 条 SQL（一次 deny、一次 allow）
→ 9548 条 SQL 查空表
```

### 为什么不能简单短路

```python
def _allowed(self, ctx, resource, *, ancestry=False):
    forbidden = _one(...)                                  # 查 1
    rules = _all(...)                                      # 查 2
    effects = {...}
    if 'deny' in effects: return False
    if ctx.mode == 'internal':                        return True   ← 早退 1
    if resource['origin_scope'] == ctx.audience_key:  return True   ← 早退 2
    if self.same_person_group_event(ctx, resource):   return True   ← 早退 3
    return 'allow' in effects and not ancestry
```

**如果加 `if 表空: return True`，会跳过早退 2、3 —— 她可能看到跨 scope 的资源。**

### 需要设计决策的

```
① life_acl 空表是"设计上就是空"（全部允许），还是"从来没写入过"（bug）？
② 如果确认空表 = 全部允许，那"安全版优化"是：
   保留三条早退判断，只跳过两条查空表的 SQL
   → 收益约 5%
③ ACL 模型要不要重新设计？（15759 个资源逐个查，结构上就是慢）
```

### 规矩（今天立的）

```
权限函数（allowed/acl/permission/auth）不许"为了性能"加减分支
如果非要优化，只能"保留全部判断，只是跳过明显无用的查询"
任何权限改动，必须列出"改前改后在 N 种场景下各返回什么"的对照表
```

---

## P8：浏览器接入 A 阶段代码未写完

### 已完成（环境，V1-V4）

```
Chrome       /opt/google/chrome/chrome  158.0.8127.36（deb 已装，dpkg -r 可卸载）
Node         /opt/node22/bin/node  v22.22.0（系统 node 18.19.1 未动）
AppArmor     /etc/apparmor.d/chrome 现成有效，一行没写
swap         /swapfile-2g 2G，已挂载、已写 fstab
swappiness   10（两处一致）
snap         已清空（只剩 snapd）

实测：companion 用户 + 保持沙箱 + headless 出真实 DOM（example.com）
内存：cgroup 口径峰值 635MB（B站首页+视频播放页+图库同开）
OOM：MemoryMax=400M 时浏览器被杀，companion 不受影响
```

**注意：swap 现在已用约 88MiB（不是"未使用"）。**

### 未完成

```
① 云端适配器（life/companion_browser.py）
   现有 life/companion_browser_adapter.py 是**桌面端版**草稿，要改成云端版
② A 阶段验收：真实模型经生产入口打开网页、读回结构化结果
③ B 阶段：只读流程学习与回放（三个任务）
④ C 阶段：变化与恢复
```

### 需要设计决策的（设计文档 §7 已给方向）

```
① systemd scope：用预配置的 companion-browser.service（单实例，User=companion，
   独立 cgroup），不让模型管理 systemd-run
② 通信：专用 Unix socket 同用户访问，不开公网端口
③ 任务生命周期：任务开始 = 首个 browser 动作被执行端接收并获得 project 绑定；
   任务结束 = 完成/取消/失败，或空闲期限到且无在途操作
④ GUIDES：普通公开正文优先 web；需要动态加载/交互/已验证 procedure 时才用 browser
```

---

## P9：`dialogue` 轮次在守卫后是否仍超时 —— 未验证

### 现状

```
今天加了 purpose 守卫：只在 purpose='dialogue' 时才跑 _prefetch_recall
验证：10 分钟内语义召回日志 0 条
但这段时间没有用户对话，所以"dialogue 轮次仍会超时"没被观察到
```

### 验证方式

```
发一条对话（/rpc chat），看：
① _prefetch_recall 有没有被调用
② 如果调用了，有没有超时日志
③ 她回答的端到端耗时
```

---

## P10：语义召回成功率从未被统计 —— 有记录但有分母问题

### 现状

```
今天已加 recall 记录（routes_detail 并入 recall slot）
但：今晚没有对话轮，11 条记录全是"非 dialogue 跳过"，合资格分母为 0
```

### 要能回答的三个数

```
① semantic 成功运行几次
② 找到匹配几次
③ 实际装入上下文几次

分母按合资格 dialogue 请求
text 跳过不算失败
不能用"日志 0 条"证明成功
```

### 查询方式

```sql
-- 见 docs/recall-minor-fixes-20261119.md 或 ops/scripts/recall_counts.py
```

---

## P11：记忆索引重建还在跑

### 现状

```
done      21836
pending    9342
running       5
unknown       5
v1 ready  27630
v2 ready  64501
速率约 16 job/分 → 剩余约 10 小时
```

### 待确认

```
① 建完之后那 9342 条会不会都成功
② unknown 那 5 条是什么原因（单发失败，不是批量崩）
③ v1 那 27630 行要不要清（现在是"暂留用于回滚，不参与 v2 查询混排"）
```

---

## 附：今天已修复的（不需要再处理）

```
① length 丢 action（四层 bug）
② 记忆索引七层死锁
③ emit 清缓存死循环（CPU 96% → 14.6%）
④ _projects LIKE 走不了索引（120ms → 2.8ms）
⑤ memory 查不到记成 failed → completed + found=False
⑥ 816 条 v1 残留标 superseded
⑦ _prefetch_recall timeout 2.0 → 0.8 + purpose 守卫
⑧ RECALL_MODE_OVERRIDE 环境变量（可回滚的 A/B 载体，含 ignore_override 诊断参数）
⑨ resolve_mode 统一入口（6/6 验证；text 下 query_vector 调用 0 次）
⑩ 每轮先清空 recall 状态（修可能的上轮残留）
⑪ recall 记录（routes_detail）
```

---

## 建议的明天顺序

```
1. P0  include_cursor=False（让 context 不付 1154 token 骨架）
    ← 修完"找记忆"才第一次真正工作，影响最大
2. P1/P2 复核 budget + scan_limit 截断改 break
3. P3/P4 召回管线（RecallReadSession → rank_candidates → search_v2）
4. P11 等索引跑完，核对覆盖
5. P5  strict mode（schema 结构，需要设计决策）
6. P8  浏览器 A 阶段（可并行，和召回无关）
```

**判据：每修完一条，要用 golden 验证"text 路线结果不变"——这是今天建立的第一份契约。**
