# 浏览器与可回放技能接入 — 最小实施计划

日期：2026-11-19（北京时间）
依据：`docs/superpowers/specs/2026-11-19-evolution-computer-use-integration-design.md`
状态：计划，未实施

## 0. 先行约束（来自用户）

- 不整体迁入 dsh-world
- 不重写现有记忆和调度
- 验收必须是"真实模型经生产入口完成三个浏览器任务"，不允许绕过校验直接调用插件
- 分别报告首次学习 / 回放 / 修复的成功情况与费用

## 1. 基线（2026-11-19 10:00 实测）

### 云端 `47.104.8.84:/opt/qq-deepseek`

```
HEAD          e08edfb
未合并变更     life/companion.py, life/embedding.py, life/local_embedding.py, life/memory_index.py
              （第六/七层记忆索引修复进行中，与本期无交集）
服务状态       active, /alive 200 @ 1.4ms
md5           companion.py            7555aa8cf63b7b2d7a442bca77494831
              companion_executor.py   f6dc731d105600c076a5279925e48823
              companion_remote.py     83cd741ab8e866624213554a6cca3bb2
              companion_context.py    39aba7441ec0ee8269bd18811823b1b6
```

### 桌面端

```
本机 C:\Users\76328\qq-deepseek   HEAD 1df0df70（`life/companion_shell_assets/` 有 host.cjs + world.ts）
完整工程                          在用户家中电脑，本次拉取不到
缺失                              package.json / vendor/coopanion / data/companion-runtime
node                              v22.14.0（上游要求 >=22.16.0，需升级）
```

**结论：本次只能交付"云端侧 + 适配器代码 + 契约测试"。真机接入与三任务验收必须在用户家中电脑上完成。**

## 2. 现有链路（已核对，非推测）

`browser` 应照抄 `computer` 的远程路径，不是 `web`（web 是云端本地执行）。

```
云端
  executor.run(action)
    kind=='browser' → RemoteExecutor.run() 分支
      server.control('browser', action)                   companion_remote.py:405
        → {"type":"remote_control","id":key,"kind":"browser","args":action}
        → await asyncio.wait_for(future, 180)
桌面端
  RemoteServiceThread 收到 remote_control                  companion_remote.py:529
    execute(m)  :551
      m['kind']=='browser' → browser adapter
        → {"type":"control_result","id":m['id'],"result":...}
云端
  收到 control_result  :462 → pending[key].set_result(result)
```

已确认的相关事实：
- `available_kinds()` 在 `companion_executor.py:131`；`RemoteExecutor` 覆写它把 `computer` 加进来
- 桌面端 capability 由 `executor_kinds` RPC 输出（含 `available`/`verified`/`verified_how`/`reason`）
- `host.cjs` 只处理 `open_chat` / `body_probe` / `dress` —— 它是另一条 127.0.0.1 通道，**不承担 remote_control**

## 3. A 阶段：最小真实链路

### 3.1 云端改动（可独立完成）

| 文件 | 改动 | 判据 |
|---|---|---|
| `life/companion.py` | DECISION schema 增加 `browser`；`_validate_action_projects` 按 op 校验（`tools` 不需 project，`call` 需要非空 `tool`） | 无 command 也能通过；缺 `tool` 被拒且报字段路径 |
| `life/companion_executor.py` | `available_kinds()` 增加 `browser`（仅在 profile 开启时） | `executor_kinds` 返回 `browser` |
| `life/companion_remote.py` | `RemoteExecutor.run()` 增加 `kind=='browser'` 分支；`execute()` 增加 `m['kind']=='browser'` 分支 | 端到端契约测试通过 |
| `life/companion_context.py` | `GUIDES` 增加 `browser`（两种 op、按 op 给参数、不要求 command） | 无"必须/应该"类规则措辞 |
| 新 `life/companion_browser.py` | 云端侧：action 校验、结果投影、artifact 引用 | 单测 |

**不做**：不改 phone、不改 computer 坐标语义。

### 3.2 桌面端适配器（新文件，需在用户电脑上落位）

```
life/companion_browser_adapter.mjs
```

职责：
- 长驻 MCP stdio 子进程（不每次 npx 启动）
- `tools/list` 读取真实工具名与 schema，**不写死**
- 按 `call` 的参数做 schema 校验后转发
- 结构化结果适配（MCP `isError` → failed；文本 → 附 artifact）
- 会话租约：空闲 2 分钟 / 单任务 10 分钟，超时禁操作

**为什么需要薄 Node 包装**：设计 §6 明确"若上游 MCP 只给文本，采用薄 Node 包装调用 JS 库并提供结构化结果"。首版用它取得 `procedure_run` 的结构化 `outward_done` / `failed_step`。

### 3.3 契约测试（不依赖真机）

用一个假的 MCP server（本地 Node 脚本，按上游 `tools/list` 的真实 schema 返回）验证：
- `tools` op 返回真实清单
- `call op` 的参数校验与错误字段路径
- `isError` 映射为 failed，不写 completed
- 超时/断连 → unknown，不重放

## 4. B 阶段：只读流程学习与回放

复用上游 `procedures/{format,run}.ts`，不重写回放算法。

**允许自动重放的集合（设计 §7 要求明确列出）：**

```
只读允许：navigate（白名单域名）/ read / extract / wait / scroll
默认禁放：click / type / submit / send / script / 任何产生提交行为的步骤
```

**白名单域名在配置里，不在代码里。**

三个验收任务：
1. 读取固定页面字段
2. 带参数搜索并提取列表
3. 跨页面汇总

## 5. C 阶段：变化与恢复

- DOM 改版 → 明确 failed_step + 页面快照
- 空结果 → 区分"合法空"与"提取失效"（需任务依据）
- 旧版回退 → 最多 2 版，标明实际使用版本
- 进程中断 → `unknown`，同 action_id 不重放
- 远程重连 → 不自动 resume

## 6. 交付顺序

```
①  云端 browser kind 全链（schema/校验/分发/投影/GUIDES）  ← 现在做
②  桌面端适配器代码 + 假 MCP 契约测试                       ← 现在做
③  用户在自家电脑：npm 装锁版本 + 升级 node + 落位适配器    ← 等他
④  真机握手 + 能力目录显示真实 ready 原因                   ← 等他
⑤  真实模型经生产入口跑三个任务，记首次学习/回放/修复费用    ← 等他
```

## 7. 不可伪造的验收边界

```
✗ "适配器单测通过" 不能写成 "browser 可用"
✗ "假 MCP 契约测试通过" 不能写成 "接通"
✗ "拆包 upsteam npm 包装通" 不能写成 "插件就绪"
✓ 只有 ⑤ 完成，才能说"可回放技能可用"
```

证据分级沿用既有规则：代码已实现 / 离线测试通过 / 在线接通 / 真机验收 / 实际发布。

## 8. 仍未覆盖（如实列出）

- 桌面端完整工程拉取不到，适配器只能"写好放这儿"，无法在本机跑
- node v22.14.0 < 要求的 22.16.0
- 三任务的具体 URL 与目标内容未定（需要用户给或她自己决定）
- D 阶段（后台窗口）不在本期
