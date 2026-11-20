# 权限路径加缓存的标准流程 — 2026-11-20

## 0. 最前面一条

**隔离验证替代不了生产链验证。**

今晚所有"我以为修好了但没修好"都从这条开始：

```
· 隔离环境验证 routes 有值（text.cand=40/ret=1）
  → 生产里 _prefetch_recall 的 timeout=0.8s，search 要 1.6s，3/3 超时
  → routes 回填那段代码从来没执行过

· 空表 ACL 短路"看起来安全"
  → 会跳过 _allowed 的三个早退分支（mode / origin_scope / same_person_group_event）
  → 自己发现、自己拦下

· scan_cache 传递"看起来只影响性能"
  → 权限路径上的缓存改动，必须证明"结果不变"才能上线
```

判读方式：**任何"隔离环境已验证"的结论，都要在真实入口上再验一次。**
本文件的流程就是这条规矩在权限路径上的落地。

## 1. 什么时候需要这个流程

改动触及以下任一函数时：

```
LifeAccess.allowed()        LifeAccess._allowed()
LifeAccess.load()           LifeAccess._dependencies()
LifeAccess.internal_allowed()
LifeCatalog.live()          LifeCatalog._live()
LifeCatalog.resolve()       LifeCatalog.parents()
```

**判断标准不是"我改的是不是权限代码"，而是"我的改动会不会让这些函数在别处被复用缓存/提前返回"。**

## 2. 五步验证

### 第 1 步：找环

祖先链上的环会让 `visiting` 检查触发，而**环的判定是路径相关的**——
`live()` 里那句注释写得很清楚：

> A cycle verdict is path dependent, so it must never be memoised.

```sql
-- life_source_edges 的真实列名：child_id / parent_id / parent_version / relation
-- （写成 resource_id 会直接报 no such column，别凭印象写）

-- 两点环
SELECT p.child_id a, p.parent_id b FROM life_source_edges p
JOIN life_source_edges x ON x.child_id = p.parent_id AND x.parent_id = p.child_id;

-- 自环
SELECT child_id, parent_id FROM life_source_edges WHERE child_id = parent_id;

-- 链深度分布
SELECT cnt, COUNT(*) n FROM (
  SELECT child_id x, COUNT(*) cnt FROM life_source_edges GROUP BY child_id
) GROUP BY cnt ORDER BY cnt;
```

**没有环 → `visiting` 不会触发 → 缓存不影响环判定。**
**有环 → 必须拿环上资源做专项对比。**

### 第 2 步：全量对比（三路）

对**全部** active 资源、**每一个有意义的 mode**，比较三路结果：

```python
a  = access.allowed(ctx, resource)                                    # 不加缓存
b  = access.allowed(ctx, resource, _cache=cache)                      # 第一次走缓存（未命中）
b2 = access.allowed(ctx, resource, _cache=cache)                      # 第二次走缓存（命中）
# 要求 a == b == b2
```

**必须比较第三路**：只比较前两路，测不出"缓存命中的结果"是否被污染。

实测覆盖（2026-11-20）：

```
audience/xiaoyu   17291 条  不一致 0   (842ms)
audience/other    17264 条  不一致 0   (848ms)
internal          17291 条  不一致 0   (809ms)
_dependencies 抽样 3000 条  不一致 0
```

### 第 3 步：不能用"差不多"过关

**任何一处不一致都要报，不能四舍五入。**

不一致的典型原因：
```
· 缓存 key 少了一个维度（mode / audience_key / ancestry / history）
· 缓存跨了 cycle 边界
· 缓存了含 clock() 的判定
```

### 第 4 步：记录未覆盖的场景

本次**未覆盖**：`diagnostic` 模式。

```
access.bind(Scope('xiaoyu','diagnostic'), mode='diagnostic')
→ RecallError: not_available      # 环境没有 diagnostic 绑定
```

**结论里必须写明"哪些场景没验过"，而不是"已验证"。**
将来有 diagnostic 场景时，要单独补一次。

### 第 5 步：部署后必须量收益

**改动的收益不能只靠推算，部署后要用同一组输入再测一次。**

本次实测：

```
context('热液')      1355 → 479ms   (-65%)   ← 与 monkey patch 预测一致
context('你还记得…')  2892 → 2138ms  (-26%)   ← 但 live() 调用数完全不变（4648）

/alive n=300:  p50=5.7ms  p95=227.5ms  p99=411.8ms
  >500ms 1 次 (0.3%)   ← 改前 27 次 (9.0%)

companion CPU: 62.4% → 38.0%（降速逻辑不再触发）
```

**-26% 那个不能算作本改动的功劳**：该查询的 `live()` 调用数一次都没变，
收益来自别的因素（可能是重启后 DB 页缓存更热）。

**归因原则：只有在"机制上能解释"的收益才记到这个改动名下。**

## 3. 这一流程救下的一次改动

```
改动：life_recall.py 的 access.load() 传 _cache=scan_cache（一行）
看起来只影响性能
实际触及：load → allowed → _dependencies → catalog.live → _live → _describe

如果只加缓存不验证：
  · _dependencies 的 memo key 不含 visiting
  · 若存在环，第一次算出的 verdict 依赖路径，缓存会固化错误的判定
  · 表现是"某些资源突然可见/不可见"——而不会崩

验证结果：环 0 处、17291 条 × 2 mode 全部一致 → 安全
```

## 4. 检查清单（可直接抄）

```
[ ] 列出改动触及的权限函数
[ ] 找环（两点环 / 自环 / 链深度）
[ ] 全量资源 × 每个 mode × 三路对比（plain / cached / cached-again）
[ ] _dependencies 单独抽样对比
[ ] 不一致数必须为 0，否则停下
[ ] 记录未覆盖的 mode / 场景
[ ] 部署后用同一组输入复测
[ ] 只有机制上能解释的收益才记到本次改动名下
```
