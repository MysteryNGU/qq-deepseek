# A 阶段交付报告：浏览器最小真实链路

日期：2026-11-19（北京时间）
依据：`2026-11-19-evolution-computer-use-integration-design.md` §11 A 阶段

## 状态

```
云端代码     ✅ 已实现并语法/导入/契约测试通过
桌面端适配器 ✅ 已实现并契约测试通过
真机接入     ❌ 未做（桌面端在用户家中电脑，本轮拉取不到）
三任务验收   ❌ 未做（依赖真机）
```

**证据分级：代码已实现 + 离线测试通过。在线接通与真机验收均未达成，不宣称可用。**

## 1. 云端改动（4 个文件）

| 文件 | 改动 | 位置 |
|---|---|---|
| `life/companion.py` | `DECISION` 的 `actions.kind` enum 加 `browser`；`_validate_action_projects` 加按 op 校验 | 437 行 / 2886 行附近 |
| `life/companion_executor.py` | `available_kinds()` 加 `browser`（条件 `_browser_ready`，默认 False） | 641 → 642 行 |
| `life/companion_remote.py` | `RemoteExecutor.run()` 加 `kind=='browser'`；桌面端 `execute()` 加 `m['kind']=='browser'`；初始化 `self.browser` | 581 → 590 行 |
| `life/companion_context.py` | `GUIDES['browser']`（388 字符）+ `GUIDES_BRIEF['browser']` | 124 → 126 行 |

### 校验契约（不重演"执行器支持 list，但入口要求必须填 command"）

```python
if action['kind'] == 'browser':
    op = action.get('op')
    if op not in ('tools', 'call'):
        raise _retryable('browser 需要 op（tools 查有哪些工具 / call 调一个工具）…')
    fields = parse_object(action.get('arguments') or '{}')
    if op == 'call' and not fields.get('tool'):
        raise _retryable('browser op=call 的 arguments 里需要 tool…')
```

**`browser` 从不要求 `command` 非空。`op=tools` 连 `arguments` 都可以没有。**

### GUIDES 措辞

含"必须/应该"类规则措辞：**False**（实测）。

## 2. 桌面端适配器（新文件）

`life/companion_browser_adapter.py`（10590 字节）

- 长驻 MCP stdio 子进程（`start()` 一次，之后复用）
- `tools/list` 读真实清单，**工具名与字段不写死**；不认识的工具名如实报现有清单
- MCP `isError` → `failed`，**不写 completed**
- `structuredContent.outward_done` 只从结构化字段读，**不从"完成。"这类文本猜**
- 超时（`browser_timeout`）→ `status='unknown'`，不重放
- 断连（`browser_offline`）→ `failed`
- 未装（`browser_not_installed`）→ 在启动子进程之前就拒绝

## 3. 契约测试结果（全部实跑）

### 适配器层（无子进程，测参数校验路径）

```
tools 无 command  → failed: browser_not_installed   ✅ 不崩，如实说
call 无 tool      → failed: missing_tool            ✅ 启动子进程前就拒绝
坏 op             → failed: bad_op                  ✅
坏 JSON           → failed: bad_arguments           ✅
available()       → False（未配 command，正确）
```

### schema 层

```
actions.kind enum 含 browser                ✅
合法 browser action 过 schema               ✅
缺 op 不过 schema → 否（op 是可选字段）      ← 由 _validate_action_projects 拦，非 schema 拦
```

**说明：`op` 在 schema 里是可选字段，这是既有设计（`web` 也一样）。按 op 校验发生在解码后，符合设计 §5"按op校验"。**

### executor 层

```python
ActionExecutor.available_kinds() 末尾：
+ (['browser'] if getattr(self, '_browser_ready', False) else [])
```
✅ 默认 False，不会在没接通时谎报可用。

## 4. 未完成（如实列出）

| 项 | 状态 | 原因 |
|---|---|---|
| 真机握手 | ❌ | 桌面端在用户家中电脑 |
| 能力目录显示真实 ready 原因 | ❌ | 需真机握手后由 `executor_kinds` 反映 |
| 三个真实任务验收 | ❌ | 依赖真机 |
| 首次学习/回放/修复的费用 | ❌ | 依赖真机 |
| B 阶段（只读流程回放） | ❌ | 依赖 A 的在线接通 |
| C 阶段（变化与恢复） | ❌ | 依赖 B |

## 5. 一个必须说清的边界

**`_browser_ready` 目前没有任何代码把它设为 True。**

这是有意的：设计 §4 要求"桌面重连发送能力握手：endpoint_id、工具版本、支持操作、浏览器可启动状态、桌面模式、halted"。

**握手协议本身还没实现**——它属于 A 阶段剩余的"真机接入"部分，需要桌面端在场才能验证。

**在此之前，`browser` 不会出现在她的可用工具里，这比"列出来但用不了"更诚实。**

## 6. 下一步（需要用户配合）

```
① 用户在家中电脑上：npm 装锁版本 evolution-computer-use（node 需从 22.14 升到 >=22.16）
② 把 life/companion_browser_adapter.py 和 companion_remote.py 同步到那台机器
③ 配 config：browser.command = 锁版本的入口命令
④ 握手：桌面端启动时发 capabilities，云端置 _browser_ready
⑤ 跑三个任务，记首次学习/回放/修复的费用与成功率
```
