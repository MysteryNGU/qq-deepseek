# 第 4 步对照表：5 处写死 → 真实数据源 → 事实层

日期：2026-11-18。**只写文档，未改代码。** 等 captain 核准后再动手。

基线（动手前现取，两端一致）：

| 文件 | md5 / sha256 | 行数 |
|---|---|---|
| `life/companion_awareness.py` | md5 `0f2a9735895ff51f06fcdc2f86b57132` | 1496 |
| `life/companion_world.py` | sha256 `c3eb5972897904b463fd1b155a7345c2adea01d642a45edef42041beac186419` | 300 |

---

## 0. 对照表

| # | 位置 | 现在写死什么 | 真实数据从哪读 | 对应 fact | 现状 |
|---|---|---|---|---|---|
| 1 | `life/companion_awareness.py:482-484` | 「**最近 90 秒内**没有上报过注册」 | `phone_context().devices` 为空 | `phone.link`（`state=offline`） | **已接**，但 `_offline_reason` 没用它，自己又手写一遍 |
| 2 | `life/companion_awareness.py:673-674` | 「手机在**最近 90 秒**内注册过」 | `phone_context().devices` 非空但无 `has_stream` | `phone.link`（`state=offline`） | 同上 |
| 3 | `life/companion_awareness.py:503-504` | 无条件断言「没启用或没有保存接入码」`→ needs_setup` | `core.town.state`（**9 个取值**，见 §2） | `town.link` | **需新建调用**，事实层已在 |
| 4 | `life/companion_awareness.py:506` | 「没有登录态（插件未配置或登录已失效）」把三种原因抹平 | `executor._bilibili.status()` 的 `available`/`credential_present`/`connected` | `bilibili.status` | **需新建调用**，事实层已在 |
| 5 | `life/companion_awareness.py:507-508` | 无条件断言「本机没有可用的 DSH 运行时」`→ needs_setup` | `_dsh_endpoint(executor)`（**三态**，见 §3） | **需新建 `dsh.runtime`** | 事实层**还没有**这一条 |

**一句话**：3、4 改调事实层即可；1、2 改由 `phone_context_fact` 生成 detail（去掉手写）；
5 需要先在事实层加 `dsh.runtime`，而它的数据源已经存在、只是没被接。

---

## 1. `:482` / `:673` —— 这不是「状态写死」，是「文案里嵌了常量」

两处都在**描述同一个事实**：手机在注册窗口内/不在窗口内。区别只是语境：

| 位置 | 所在函数 | 语义 |
|---|---|---|
| `:482` | `_offline_reason` | 手机**不可用**，解释为什么 |
| `:673` | `_dispatch_caveat` | 手机会被**报成可用**，但派活会被拒 |

**captain 的判断是对的**：这两处应该由 `phone_context_fact()` 生成 detail，
而不是在 `_offline_reason` 里手写一遍。否则同一个事实有两处措辞，将来必然漂。

**但有一个必须处理的差异**：两处的**动作建议不同**，不能直接共用同一句：

- `:482` 的建议是「打开 App 并保持前台」（因为完全没有注册）
- `:673` 的建议是「先确认 App 在前台再操作」（因为注册过但没有活动连接）

**建议做法**：`phone_context_fact()` 的 `detail` 只说**事实**（「最近没有上报过注册」／
「注册过但没有活动连接」，**不含秒数**），两处各自追加自己的**建议**。

常量 `RECENT_SECONDS=90`（`life/companion_phone.py:22`）放进 `raw`，
需要精确值时按需读取能拿到——这正是 `raw` 的用途。

---

## 2. `:503` town —— 真实数据源的细节（**这里有个坑**）

`core.town.state` 的实际取值（从 `life/companion_town.py` 提取，共 **9 个**）：

```
closed, config_error, connecting, disabled, invalid_code,
needs_account, online, paused, reconnecting
```

事实层现有映射覆盖了 8 个，**漏了 `closed`**：

```python
mapping = {'online': ('ready', ''),
           'disabled': ..., 'config_error': ..., 'needs_account': ...,
           'invalid_code': ..., 'paused': ..., 'connecting': ...,
           'reconnecting': ..., 'error': ...}     # ← 'error' 有，'closed' 没有
```

`closed` 落到 `unknown`（「状态没对应说法」）——**这不算错**（读不懂就不猜），
但它是**已知**状态，应该给一句准确的话。所以第 4 步之前要先补这一条映射。

**另一个候选数据源我查了，不推荐**：

- `town.snapshot()` —— 它返回 `state=self.state`，**和 `.state` 同源**，但多扫了两张表
  （`companion_usage` / `companion_town_turns`）做统计。为了取一个 `state` 去跑这些查询不划算。
- `town.config.public()` —— 返回 `enabled` / `has_code`，那是**配置**不是**运行状态**。
  `:503` 现在写死的正是这两个配置项，改成读它等于没改（还是「配置怎么说不等于现在怎样」）。

**建议**：直接读 `core.town.state`（事实层 `town_fact()` 已经是这么做的）。

---

## 3. `:508` dsh —— 你点名要我确认的那一行

### `_dsh_endpoint()` 在哪、返回什么

**位置**：`life/companion_awareness.py:371-383`

```python
def _dsh_endpoint(executor):
    """沿执行器链找到真正持有 DSH 配置的那一层；拿不到就是未知（None）。"""
    seen = 0
    while executor is not None and seen < 4:
        paths = getattr(executor, '_dsh_paths', None)
        if callable(paths):
            try:
                return paths() is not None      # ← True / False
            except Exception:
                return None                     # ← 探测抛错
        executor = getattr(executor, 'original', None)
        seen += 1
    return None                                 # ← 走完 4 层没找到
```

### 能不能直接判断「有没有可用运行时」

**能，而且它已经是三态**，正好对上事实层的三档：

| 返回值 | 含义 | 对应 `state` | 依据 |
|---|---|---|---|
| `True` | `_dsh_paths()` 非 None，运行时可用 | `ready` | `executor._dsh_paths()` 返回了路径三元组 |
| `False` | 探测到了，明确不可用 | `needs_setup` | `_dsh_paths()` 返回 None（root/node/entry 缺一） |
| `None` | 读不到（抛错，或链上 4 层都没有 `_dsh_paths`） | `unknown` | 不能断言 |

**这正是今晚 F-2 那条规矩的典型范例**：`None` 与 `False` 必须分开，
`None` 就是「读不到」，不能说成「本机没有运行时」——而 `:508` 现在正是把它说成了后者。

### 需要新建 `dsh.runtime`

事实层目前没有 dsh 条目。加一个 `dsh_fact(executor)`：

```python
def dsh_fact(executor, *, now=None):
    endpoint = _probe_dsh(executor)          # 三态，复用 awareness 的走链逻辑
    if endpoint is None:  return unknown('dsh', '读不到 dsh 路径探测', fact_ref='dsh.runtime')
    if endpoint:          return fact('dsh.runtime', 'dsh', 'ready', detail='本机有可用的 DSH 运行时')
    return fact('dsh.runtime', 'dsh', 'needs_setup', detail='本机没有可用的 DSH 运行时（dsh_root/dsh_node 或入口缺失）')
```

**注意**：`_dsh_endpoint` 在 `life/companion_awareness.py` 里。事实层不能反向 import awareness
（会成环）。两个选项：

- **A（建议）**：在 `companion_world.py` 里**复制那段走链逻辑**（14 行），并加测试断言两者行为一致；
- B：让 `_offline_reason` 调事实层，事实层接收一个已经算好的 `endpoint` 参数（由调用方传入）。

选 A：走链逻辑很短、无副作用；B 会让事实层多一个「必须由调用方预先算好」的隐式契约。

---

## 4. 改动面预估（先给你看规模，再决定要不要拆）

| 文件 | 改动性质 | 预估 |
|---|---|---|
| `life/companion_world.py` | 新增 `dsh_fact()`；补 town `closed` 映射；`collect()` 加 dsh 一条 | **+约 45 行**，纯新增 |
| `life/companion_awareness.py` | `:482`/`:673`/`:503`/`:506`/`:508` 五处改为调事实层 | **改约 25 行，删约 12 行** |

**第二项会删代码**（写死文案被替换）。这是本次唯一"减法"，
我会逐处 diff 给你看，不整体写回。

---

## 5. 验证计划（改完必跑）

1. **主上下文长度**：改前基线 **6270 字符**（`_messages` 的 state 段），改后 ≤ +10%
2. **能力目录不退化**：`channel.qq` 仍 `offline` + `usable=false` + reason 非空
3. **真实问答**（你的最终验收）：
   - 「小镇能用吗」→ 状态正常时必须说"能用"，不能报 `needs_setup`
   - 「B站为什么不能用」→ 必须区分插件没装 / 没扫码 / 掉线
4. **每处一条测试**：状态正常的夹具上返回 `needs_setup`/`offline` 就算违规
5. **变异验证**：把事实层改回写死，测试必须红

---

## 6. 还没确认、需要你拍板的三件事

1. **`closed` 该映射成什么？** 我倾向 `offline`（小镇关掉了，不是没配置）。
2. **`_dsh_endpoint` 走链逻辑：复制（A）还是参数传入（B）？** 我选 A。
3. **`:673` 的建议语与 `:482` 不同**——是否接受「事实共用、建议各自追加」这个划法？
