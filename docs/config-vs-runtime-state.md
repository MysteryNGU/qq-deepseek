# 配置事实 ≠ 运行状态 ≠ 初始值

2026-11-18。三个 bug，同一个根。

---

## 一句话

> **三者都可读，但只有「运行状态」能回答「现在能不能用」。**

| 类别 | 例子 | 何时可信 | 能回答什么 |
|---|---|---|---|
| **配置事实** | `town.config.enabled`、`channel` 配置文件 | **任何时候** | 「有没有人把它配好」 |
| **运行状态** | `town.state == 'online'`、`server.peer` | **只在组件启动后** | 「现在能不能用」 |
| **初始值** | `_body_context = dict(connected=False)`、`state='disabled'` | 组件启动前读到的就是这个 | **什么都不能回答** |

**把前两者当成第三者，或者把第三者当成第二者，就会说错话。**

---

## 今晚的三个实例

### 实例 1：`_body_context` 的初始值被当成状态

```python
# life/companion.py:531  构造时
self._body_context = dict(connected=False)

# life/companion.py:1109  只有桌面桥真的推来状态才覆盖
async def update_body(self, context):
    self._body_context = dict(context)
```

**问题**：`connected=False` 有两个含义，字典本身分不出来：
- 桌面桥从没连过（初始值）
- 桌面桥连过、现在断了

**后果**：一律说成 `offline`「身体形象没有连上」——把「不知道」说成「知道」。
这正是 F-2 修的同类缺陷。

**修法**（`life/companion_world.py::body_fact`）：看有没有「桥推过状态」的痕迹
（`figure` / `position` / `motion` / `can_walk` 等会残留在字典里）：

| `_body_context` | 判定 |
|---|---|
| `connected=True` | `ready` |
| 有痕迹但 `connected=False` | `offline`（连过，断了） |
| 只有 `{'connected': False}` | **`unknown`**（从没连过） |

### 实例 2：`town.config.enabled` 被当成可用性

```python
# life/companion.py:1706（修前是 1688，B1 加了 try 后行号后移）
if self.town.config.enabled and self.town.config.code:
    kinds.append('town')          # ← 只看配置，不看桥起没起
```

**问题**：配置好 ≠ 连得上。而 `town.state` 又有个陷阱：

```python
# life/companion_town.py:172  构造时
self.state = 'config_error' if self.config_error else 'disabled'

# :411  set_active() 才推进；:442 _bind() 成功才变 online
```

**所以 `state == 'disabled'` 也有两个含义**：真的没启用 / 桥刚构造还没 start。

**后果**：能力目录说「能用（去小镇和角色互动）」，而事实层说「不能确认」。
**方向是最危险的那种**——她以为能用，用户去试才失败。

**修法**（`life/companion_world.py::town_fact`）：**配置定性在先，状态判连接在后**。

| 条件 | 判定 |
|---|---|
| `enabled=False` | `needs_setup`「没启用」 |
| `code` 空 | `needs_setup`「没有接入码」 |
| `state` ∈ (connecting, online, paused, reconnecting, error) | 按状态映射 |
| **`state=='disabled'` 但配置是好的** | **`unknown`「桥还没开始连」** |

并同步修正 `life/companion.py:1706` 的 `kinds` 门槛（A），让目录与事实层同源。

### 实例 3：`_started=True` 早于组件启动

```python
# life/companion.py:916
self._started = True
...
# life/companion.py:929
await self.town.start()        # ← 抛错就冒到 start()
```

**问题**：`start()` 开头有 `if self._started: return`。桥起失败 → `_started` 已是 True →
**重试 `start()` 直接返回，桥永远起不来，且没有日志。**

**实测复现**：

```
第一次 start()：town.start() 抛错 → _started=True, town.state=disabled
第二次 start()（改成正常）          → 直接 return, town.state 还是 disabled
```

**修法**（B1）：给 `town.start()`（现在是 `:929`）包 try，失败写 `log.exception` **并写进 `companion_activity`**
（只写 journal 会在滚动后丢失）。**没有**改 `_started` 的位置——因为 `:917-921` 的后台任务（那一带现在在 `:920-925`）
也在它之后，挪动会导致重试时建两套 scheduler/input/worker。

---

## 同类风险清单（未修，供后续）

按「配置/初始值可能被当成状态」逐个过了一遍：

| 位置 | 现状 | 风险 |
|---|---|---|
| `life/companion_bilibili.py::status()` | 三个布尔都是**实时**读（文件存在 + 进程 `returncode`） | **低**——它本来就读运行状态，没有初始值陷阱 |
| `life/companion_phone.py::context()` | `devices` 来自实时注册表 | **低**——空列表 = 真的没有设备 |
| `life/companion_awareness.py::_dsh_endpoint` | 三态 `True/False/None` 已正确 | **无**——已分开 |
| `life/companion.py:1708` `if self._body and self._body_context.get('connected')` | 只看 `connected` 布尔 | **中**——桥没连过时 `body` 不进 `kinds`，与实例 2 同形。**当前无害**（不进 kinds 走 `_offline_reason`，那里已用 `body_fact` 的三态），但没有显式说明 |
| `life/companion.py:912` `if hasattr(cfg,'companion_autonomy')` | 键不存在就跳过 `set_active` | **低**——但意味着「配置缺失」被当成「保持默认」，不是「未配置要报错」 |

**结论**：`phone` / `bilibili` / `dsh` **没有**同类严重问题（它们要么本来就实时读，要么已经是三态）。
唯一同形的是 `body` 在 `:1708` 的 `kinds` 门槛，但它下游已经走三态判据，**结果正确**，
只是没有像 town 那样显式写清楚。

---

## 怎么写才对

### 判断「现在能不能用」时

1. **先问「这个值是哪一类？」** 配置 / 运行状态 / 初始值
2. **初始值一定不能直接当结论**——要么找伴随证据（如 `body` 的痕迹字段），
   要么老实说 `unknown`
3. **配置只能回答「配没配」**，回答不了「通不通」
4. **两个都要用**：配置定性（要不要人去改）、状态判连接（现在能不能用）

### 声明读的是哪一份

代码注释里写清：`config.enabled`（配置事实，任何时候可读）
vs `state`（运行状态，只在 start 后有义）。**别只写"读 town 的状态"。**

### 测试判据

每条都要有：**状态正常时返回 `needs_setup`/`offline` 就算违规**；
反过来，**读不到时返回确定结论也算违规**。两个方向都要测。

---

## 这条原则为什么重要

今晚出现频率最高的一类 bug 是「**桩能过但真实结构不对**」：

- `_bilibili` 挂在 `ActionExecutor` 自己身上，而我按 `original` 找 → 生产永远找不到后端
- `_workers()` 死循环（桩测不出）
- `queued` vs `pending`（单测用固定数据，测不到真实状态词）
- **以上三个实例**

它们有同一个形状：**测试和代码都自洽，但和真实的运行状态不一致。**

最直接的解法不是更复杂的测试，而是：**能看进程/连接的时候，别只读代码。**

```bash
ss -tnp | grep <pid>        # 它到底连着什么
getent hosts <域名>          # 那个 IP 是不是它
curl -s localhost:8765/alive
```

今晚我用两张表 + journal 推了一小时「桥没在跑」，一条 `ss` 就推翻了。
