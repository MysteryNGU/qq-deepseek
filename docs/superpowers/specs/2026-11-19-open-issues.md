# 遇到的问题清单（交接给设计）

日期：2026-11-19 20:20（北京时间）
基线：云端 `47.104.8.84:/opt/qq-deepseek`，HEAD `e76b345`
用途：这些是今天（以及遗留）**已经定位、但还没解决或没方案**的问题，交给你做设计决策。

---

## 0. 当前生产状态（供参照）

```
服务      active / HEAD e76b345
CPU       30%（含索引）
/alive    1.38s（这一下偏慢，中位通常 2~7ms）
env       RECALL_MODE_OVERRIDE=text   ← 今天的临时措施
索引      done 20547 / pending 9895 / running 5 / unknown 4
v2 索引行 52740
active 资源 15872
```

---

## P1：语义召回在 auto 模式下不可用 —— **需要重新设计**

### 现状

```
auto 模式下一次 search：5886ms
  其中 allowed() 3632ms（62%）
  其中 text_route/semantic/related 2254ms（38%）
_prefetch_recall 的 timeout 曾是 2.0 秒 → 必然超时
今天已把 timeout 降到 0.8 秒，并加 purpose 守卫（非 dialogue 不跑）
→ 结果是「不报错了」，但语义召回事实上没有产出
```

### 根因（已量化）

```
allowed() 对 15759 个资源逐个做 ACL + event 查询：
  events              13179 条 (24.7%)
  life_epochs         12433 条 (23.3%)   ← 纯重复（全局版本号）
  life_acl             9548 条 (17.9%)   ← 表是空的，查了 9548 次
  life_resources       5331 条 (10.0%)
  life_source_edges    4460 条 ( 8.4%)
  life_terms           3679 条 ( 6.9%)
  life_semantic_index  2250 条 ( 4.2%)
  合计                53354 条
```

**已试过、已证明没用：**
```
× 降 budget（8000→800）：只省 0.6 秒，因为 budget 只影响返回条数，不影响候选集
× N+1 改批量 IN (...)：更慢（0.7x），因为 IN 多前缀走不了索引
× 计算优化：计算只占 30% 左右
× 调 timeout：优化到极限 3.2~3.8 秒，仍超 2 秒预算
```

### 需要设计决策的

```
① 语义召回要不要留？
   如果留 —— 需要换设计（ANN 索引 / 离线预计算 / 缩小候选集 / 后台化）
   如果不要 —— 就正式下线 semantic 路线，而不是"留着但永远超时"
② 如果要留，候选集怎么缩小才不退化？
   今天否掉了"随便取 N 个"（会静默降级，老的/新的记忆永远搜不到）
   可行的方向：ANN 向量索引、按 scope/时间窗分批、或者预计算 top-N
③ 后台化（发起不等待，下一轮用结果）值不值得？
   分析：SQLite 单连接 + RLock，后台化只是把等待移到线程，总时间不变
        收益是"不阻塞事件循环"，不是"语义召回能用"
```

---

## P2：`life_acl` 是空表，但每个资源查两次 —— **权限语义待确认**

### 现状

```
life_acl 表：0 行
但 _allowed() 对每个资源查 2 条 SQL（一次查 deny 规则、一次查 allow 规则）
auto 模式下 9548 条 SQL，全部查空表
```

### 为什么不能简单短路

```python
def _allowed(self, ctx, resource, *, ancestry=False):
    forbidden = _one("SELECT 1 FROM life_acl WHERE ... effect='deny' AND audience_key='*'")
    if forbidden: return False
    rules = _all("SELECT effect FROM life_acl WHERE resource_id=? AND purpose=? ...")
    effects = {row['effect'] for row in rules}
    if 'deny' in effects: return False
    if ctx.mode == 'internal':                        return True   ← 早退 1
    if ctx.mode == 'diagnostic':                      return 'allow' in effects
    if resource['origin_scope'] == ctx.audience_key:  return True   ← 早退 2
    if self.same_person_group_event(ctx, resource):   return True   ← 早退 3
    return 'allow' in effects and not ancestry
```

**如果加 `if 表空: return True`，会跳过早退 2、3 —— 她可能看到跨 scope 的资源。**

### 需要设计决策的

```
① life_acl 空表是"设计上就是空"（全部允许），还是"从来没写入过"（bug）？
   → 这决定要不要给它写数据，还是可以让代码显式跳过
② 如果确认空表 = 全部允许，那"安全版优化"是：
   保留三条早退判断，只跳过两条查空表的 SQL
   → 收益约 5%，要不要做？
③ ACL 模型要不要重新设计？（现在 15759 个资源逐个查，结构上就是慢）
```

---

## P3：事件循环里做同步 DB —— **结构性技术债**

### 现状

```
主线程 62% 时间在同步 sqlite（Repository._all 37.7% + _one 24.5%）
Repository = 单连接 + threading.RLock，所有查询串行
/alive 和 /rpc chat 在同一个事件循环
→ 慢查询会让她说话卡、健康检查超时
```

### 已缓解

```
今天修了 emit 清缓存的死循环（CPU 96% → 14.6%）
今天把 mode 改 text（/alive 峰值中位 3812ms → 1022ms）
```

### 需要设计决策的

```
① 要不要做读写分离？（SQLite WAL 模式读可以并发，但现在单连接）
   风险：读到不一致的数据
② 要不要把慢查询挪到线程池？
   问题：我会阻塞成"占线程"，而且 6 个 worker（nproc=2）容易占满
③ 要不要给 Repository 加连接池 / 或按用途分连接？
④ 根本问题：为什么一次 recall 要 53354 条 SQL？
   是数据结构问题（ACL 逐资源）还是查询设计问题（N+1）还是别的？
```

---

## P4：记忆索引重建还没跑完 —— **预计 7~8 小时**

### 现状

```
done      20547
pending    9895
unknown       4
v2 索引行 52740
active 资源 15872

速率约 16 job/分 → 剩余约 10 小时
```

### 需要确认的

```
① 建完之后，那 9895 条会不会都成功？
② unknown 那 4 条是什么原因？（单发失败，不是批量崩）
③ 建完之后要不要把 v1 的 27630 行清掉？（占空间，但可能有历史价值）
```

---

## P5：`dialogue` 轮次在守卫之后是否仍超时 —— **未验证**

### 现状

```
今天加了 purpose 守卫：只在 purpose='dialogue' 时才跑 _prefetch_recall
验证：10 分钟内语义召回日志 0 条
但这段时间没有用户对话，所以"dialogue 轮次仍会超时"这一点没被观察到
```

### 需要确认的

```
发一条对话，看：
① _prefetch_recall 有没有被调用
② 如果调用了，有没有超时日志
③ 她回答的端到端耗时
```

---

## P6：语义召回的成功率从未被统计过 —— **加记录才能回答**

### 现状

```
成功时不打日志，只有超时才打
所以无法统计"语义召回到底有没有成功过"
今天有 99 次/60 分钟的超时记录，但没有成功记录
```

### 需要设计决策的

```
① _recall_status 落库到哪里？（companion_activity.details / events.payload / 新表）
② 落库频率？（每轮一条会很多，只记成功/失败？）
③ 要能回答的三个数：
   - auto 下 semantic 成功几次
   - 超时几次
   - text 下跳过几次
```

---

## P7：浏览器接入 —— **V1-V4 完成，A 阶段代码未写完**

### 已完成（环境）

```
Chrome       /opt/google/chrome/chrome  158.0.8127.36（deb 已装，可 dpkg -r 卸载）
Node         /opt/node22/bin/node  v22.22.0（系统 node 18.19.1 未动）
AppArmor     /etc/apparmor.d/chrome 现成有效（profile 是系统预置的，一行没写）
swap         /swapfile-2g 2G，已挂载、已写 fstab，实测未被使用
swappiness   10（/etc/sysctl.d + /etc/sysctl.conf 两处一致）
snap         已清空（只剩 snapd）

实测：companion 用户 + 保持沙箱 + headless 能出真实 DOM（example.com）
内存：cgroup 口径峰值 635MB（B站首页+视频播放页+图库同时开）
OOM：MemoryMax=400M 时浏览器被杀，companion 不受影响
```

### 未完成

```
① 云端适配器（life/companion_browser.py）—— 我今天自己写了个草稿
   life/companion_browser_adapter.py（桌面端版），需要改成云端版
② A 阶段验收：真实模型经生产入口打开网页、读回结构化结果
③ B 阶段：只读流程学习与回放（三个任务）
④ C 阶段：变化与恢复
```

### 需要设计决策的

```
① systemd scope 由谁创建？（适配器调 systemd-run --scope vs 预配置 browser@.service）
② 浏览器任务的生命周期怎么定义？（什么算"任务开始/结束"）
③ 静态页面优先用现有 web、动态页面才启动浏览器 —— 这条怎么在 GUIDES 里表达？
```

---

## P8：几处说明与实现对不上 —— **今天第六、七次发现**

### 已知

```
① memory_index.py:99 注释写 "unknown/paused jobs are reconciled separately"
   但 reconcile 实现是今天才补的（之前不存在）
② 今天发现 _prefetch_recall 的注释写 "不可用或超时都只是少一路候选，不拖住聊天"
   但实现里每轮白等 2 秒 —— 实现违背了自己的注释
```

### 需要设计决策的

```
① 要不要做一次"注释 vs 实现"的全量核对？
② 如果要，用什么判据？（比如：所有 "will/must/should" 的句子都要有对应代码）
```

---

## 附：今天已修复的（供对照，不需要再设计）

```
① length 丢 action（四层 bug）
② 记忆索引七层死锁
③ emit 清缓存死循环（CPU 96% → 14.6%）
④ _projects LIKE 走不了索引（120ms → 2.8ms）
⑤ memory 查不到记成 failed → completed + found=False
⑥ 816 条 v1 残留标 superseded
⑦ _prefetch_recall timeout 2.0 → 0.8 + purpose 守卫
⑧ RECALL_MODE_OVERRIDE 环境变量（可回滚的 A/B 载体）
```

---

## 需要你决策的优先级建议

```
P1（语义召回）      最需要设计，因为没有它"她记得住最近的事"是打折的
P3（同步 DB）       影响面最大，但改动风险最高
P2（ACL）           涉及权限语义，必须谨慎
P7（浏览器）        有清晰的分期，按 A→B→C 走即可
P4（索引）          时间问题，等它跑完
P5/P6               验证与可观测，工作量小
P8                  长期卫生
```
