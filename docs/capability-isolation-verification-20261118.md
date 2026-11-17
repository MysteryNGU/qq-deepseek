# t5 隔离验证：能力链路的机器可判定部分

日期：2026-11-18 00:28–00:37 CST　执行：isolation-verifier（t5）
证据目录（云端）：`/opt/qq-deepseek/.acceptance/20261118-002824-cap/`
可复跑脚本：`ops/verify-capability-chain.py`（同目录一份）、`ops/repro-used-never-empty.py`

结论先行：**a1–a4 的成果在隔离环境里可复现；能力链路的第 1、3、5 环节这次是「通」的，
第 2 环节（状态→说明）还剩三处不一致，第 4 环节（调用）剩一处措辞级缺陷。**
新发现 1 个此前没人报过的缺陷（`op=used` 摘要句），已给最小复现。

---

## 0. 隔离边界与自证

| 边界 | 做法 | 实测证据 |
|---|---|---|
| 临时 HOME | `HOME/USERPROFILE/XDG_*/TMPDIR/TEMP/TMP` 全部指向 `--out/tmp`，**再** import `life.*` | `report.txt` §env：`home_is_sandbox=true`，`HOME=/opt/qq-deepseek/.acceptance/20261118-002824-cap/tmp/home` |
| 生产库只读 | SQLite backup API，源 `mode=ro`；复制前后比 size+mtime_ns+sha256 | `db_copy.unchanged=true`；`db_source_after` 前后 size/mtime 相同；副本以 0444→0644 只用于本进程读取 |
| 不发 QQ / 不连模型 | `FakeModel`；`QQ_OFFLINE=1`；凭据类环境变量全部删除 | `env.QQ_OFFLINE=1` |
| 不重启服务 / 不碰 systemctl | 跑测试期间 `subprocess.Popen`、`asyncio.create_subprocess_*`、`socket.connect` 一律抛 `ExternalCallBlocked` 并记账 | `guard attempts: 24`，**全部**是 `systemctl is-active/is-enabled companion-watchdog.service`（t3 的 `guard.watchdog` 在探测守护状态）。被测代码因此拿到 `None`，按设计落 `unknown` |
| 不读生产 config.toml | `Config` 由脚本内 `CONFIG_TRANSCRIPT` 逐项转述，api_key 留空 | 脚本头部注释 |
| 生产库写入 | 只在**副本**里插 3 条测试动作 | 生产源文件 sha256 前后一致 |

服务运行状态未被动过：`companion.service` 全程 active（开工前查过一次，之后未再操作）。

**基线漂移（必须说明）**：本轮开始 00:09 时，`life/companion_awareness.py`（10c9fd15…）、
`life/companion_context.py`（271de14a…）与本地逐字节一致；到 00:38 复审时，云端这两份
已变为 `2ab8f079…` / `6c6756e5…`，`life/companion.py` 也从 `7e15be30…` 变为 `c4dc0610…`。
也就是说**验证期间有队友在改并部署了这三份文件**。因此：

- 本文所有结论都针对 **00:28–00:37 实际被验的那一版**（`--out` 目录里 `verification.json`
  的 `started/finished` 字段就是这段时间）；`report.txt` 里每条的 `checked_at` 也是那一刻；
- 文中给出的行号（`companion_awareness.py:437-453` 等）在那两版里都成立，但不能保证新版本沿用；
- **已在 00:37:44–00:37:56 对当时的云端代码复跑过一次**（`..20261118-002925-cap/`，
  `life/companion.py=c4dc0610…`、`companion_awareness.py=10c9fd15…`、`companion_context.py=6c6756e5…`）：
  六组结论**与 00:28 那一轮逐条一致**（C1 两个清单相同、C2 同样的三处不一致、C3 654 字符、
  C4 `used` 同一句、C5 六项检查同一结果），说明这三份文件那次改动没有碰这些路径；
  之后若再有部署，请对当前 HEAD 复跑一次（约 12 秒）。

---

## 1. 判据与结果总表

| 组 | 判据 | 结果 | 证据位置 |
|---|---|---|---|
| C1 | 代码有的每项能力都能在模型可读说明里找到 | **通过** | `verification.json#c1.code_without_doc = []` |
| C1 | 说明里写的每项都真实可调用 | **基本通过，1 处例外** | `c1.doc_without_code = ["channel.qq"]` |
| C2 | 假连接器造 ready/offline/needs_setup/unknown，状态如实 | **4/6 场景通过 + 1/2 手机场景通过** | `report.txt` §C2 |
| C3 | 装配出的能力段是「可用/不可用/按需了解」三段且不超预算 | **通过** | §C3：`three_layers=true`、`capability_section_chars=654`（上限 4000）、整轮 `7155/16000` |
| C4 | 模型被指去读的读口能真读到细节 | **通过**（含分页、过滤、not_implemented 语义） | §C4 |
| C5 | a4 经验记忆在隔离环境复现 | **通过** | §C5：`read_back_same_source=true`、`last_receipt_written=true`、`no_new_tables=true` |
| C6 | 经验库里真有过这次测试动作 | **通过** | §C5 `read_back` 返回当时原文 |

跑法（云端，companion 用户）：

```
cd /opt/qq-deepseek
sudo -u companion env -i HOME=/tmp/x PATH=/usr/local/bin:/usr/bin:/bin \
  PYTHONPATH=/opt/qq-deepseek .venv-companion/bin/python -B -X utf8 \
  .acceptance/20261118-002824-cap/verify-capability-chain.py \
  --out .acceptance/<新时间戳>-cap --prod-evidence
```

`--prod-evidence` 的含义：`agent.subagent` 的 ready 由「台账里有没有一条 completed 的
子 agent 任务」决定、`action.dsh` 由「有没有 DSH 入口」决定。临时 HOME 里这两样天生没有，
不加这个开关会把它们误报成 `needs_setup`（第一轮就是这个问题，记者是脚本的临时接线缺 `attach_core`，
见 §7 第 1 条）。加了之后，这两项从**生产库副本**里取真实值，DSH 探测按生产有入口来算。

---

## 2. C1 清单完整性：代码 ↔ 说明

**代码侧的全集**（三者并集，全部来自运行时代码，不靠文档）：

- 执行器当轮自报 `available_kinds()`：`command, skill, work, memory, growth, dsh, web, computer, bilibili, phone`（`life/companion_executor.py:94-103`，经 `life/companion_remote.py:82-96` 叠加 `computer/phone`）
- 决策 schema 允许的 `kind` 枚举（13 条，`life/companion.py:361-364`）：`command, work, dsh, agent, computer, body, bilibili, phone, town, web, skill, memory, growth`
- 掉线仍要留条目的登记表 `TRACKED_KINDS`（13 条，`life/companion_awareness.py:472-473`）

**说明侧**：`GUIDES` 11 段（`life/companion_context.py:24-38`）∪ 能力摘要 `usable/unusable` 里的名字 ∪ 能力目录条目的 `capability_ref` ∪ `AGENT_INSTRUCTIONS` 里的 `kind=agent`。

**结果**

- `code_without_doc = []`：13 个 kind 全部能在某处被模型读到。其中最弱的是 `agent`——
  它没有 `GUIDES` 段，只在 `life/companion.py:330-342` 的 `AGENT_INSTRUCTIONS` 里（该段每轮都注入）；
  能力目录里它是 `agent.subagent` 条目，这一轮为 `needs_setup`（台账里**没有一条 completed 的子 agent 任务**，
  生产库副本里 `companion_agent_tasks` 只有 1 条 `interrupted`）。这是如实状态，不是缺陷。
- `doc_without_code = ["channel.qq"]`：`channel.qq` 是通道条目，不是动作 kind，本来就不可派发；
  但它出现在摘要的「当前可用」里（`channel.qq：消息通道（已登录，能收发）`），
  和 `web：…`、`command：…` 排在一起。**对模型来说这读起来像一种能做的事**。它和「能不能发 QQ 消息」
  这层判据是对的（`usable` 会区分），只是列表里动作与通道混排。低危，见 §6 F-1。

---

## 3. C2 状态真实性（假连接器）

四个基础场景 + 两个手机专项，全部用真 `Companion` + 假 `RemoteExecutor` 形状（`server.peer / mobile.phone / im`）。

| 场景 | 造的状态 | 期望 → 实得 |
|---|---|---|
| ready | QQ 已登录、微信未配置、手机有流、桌面 peer 在、DSH 在 | 8/8 通过 |
| offline | QQ 掉线、微信要登录、手机 0 设备、桌面 peer=None | 5/5 通过，每条都带原因与 `state_scope` |
| needs_setup | QQ 配置里关掉、微信未配置 | 3/4 通过，**`action.phone` 不一致**（见下） |
| unknown | 通道快照抛异常 | 1/3 通过，**两个 channel 不一致**（见下） |
| phone_registered_no_stream | 手机注册过（`connected=true`）但 `has_stream=false` | 通过（`ready`，与执行器自报一致；t1 记的「能派活」判据另说） |
| phone_unavailable | `phone_context()` 抛异常 | **不一致**：落 `unknown`，原因写「本机没有手机控制通道。」 |

三处不一致，逐条给落地位置：

**F-2（中）`_offline_reason` 对 phone 的处理与可用性判定含义不同。**
`life/companion_awareness.py:437-453`：

```
if context is None:  return '本机没有手机控制通道。', 'unknown'
devices = context.get('devices') or []
if not devices:      return '…最近 90 秒内没有上报过注册…', 'offline'
if any(has_stream):  return '…拿不到回执，可能被系统挂起了。', 'unknown'
return '…没有活动连接…', 'offline'
```

- 这条原因**只在 `phone` 不在 `available_actions` 时才被用来解释**；而 `RemoteExecutor.available_kinds()`
  只看 `phone_context()['connected']`（`life/companion_remote.py:84-86`）。于是「注册过但没有流」
  时执行器报 `phone` 可用 → 条目走 `ready`、**没有任何提示**；同一时刻 `_offline_reason` 手里握着
  「没有活动连接」这句真话，却因为走不到而不会被说出口。t1 F9 记的「看起来连着 ≠ 能派活」在能力目录里
  **仍然没有出口**。
- `context is None`（探测本身失败）被写成「本机没有手机控制通道」——这是一句断言，
  而事实只是「问不到」。按文件头第 2 条硬规矩（未知就是未知），这里应该给 `unknown` + 「探测失败」。

**F-3（中）通道 unknown 被说成「被关掉」。**
`life/companion_awareness.py:137-218` 的 `_channel_facts`：快照拿不到时**退回到配置文件**判状态；
配置里 `enabled=false` 就报 `disabled`。实测：

```
unknown 场景  channel.qq -> offline | scope=disabled | 通道被关闭了（配置里 enabled=false），不是掉线，是主动关的。
```

但这一场景里快照是**抛异常**的，真实情况是「不知道」。`_channel_facts` 自己的注释写着
「拿不到就是 unknown，不猜」——代码却没做到：`snapshot_error` 被记下来了，但没有影响 `state`。
模型据此会说「QQ 是我自己关的」，而实际是通道状态读不到。`wechat` 同病（`unconfigured` 也会落成
`disabled`/`unconfigured` 之外的判断）。

**F-4（低）needs_setup 场景里 `action.phone` 实为 ready。**
该场景 QQ 被关、微信未配置、手机只有配对没有流。`phone` 在 `available_actions` 里 → `ready`。
这和 F-2 同源；记在这里是因为它正好是「断开某终端后她能否准确解释变化」的判据面：
**通道全断、手机只剩配对时，能力段里 phone 仍然显示可用。**

---

## 4. C3 概览格式（跑真 `_messages`）

主场景（生产库副本 + 生产形状 HOME，QQ 已登录、手机有流、桌面与 DSH 在）：

```
可用    web：上网搜索、读网页
        command：在云主机上跑命令
        skill：查和存自己的做法库
        work：读写工作区文件、跑测试
        memory：回查自己做过的动作和回执
        growth：记自己的认识与兴趣
        computer：操作主人电脑桌面
        phone：操作手机界面
        dsh：把多步工作整体交给后端代理
        channel.qq：消息通道（已登录，能收发）
不可用  body offline：桌面身体形象没有连上，动作发过去没人接收。
        town needs_setup：小镇没启用或没有保存接入码（town.config.enab…
        bilibili needs_setup：B 站没有登录态（插件未配置或登录已失效），读不到内容也不能…
        另有 3 条现在用不了，原因与用法用 memory op=capabilities 读，不要说成不存在。
按需    how：要某个能力的用法，用 memory op=capabilities 读整条；能不能用要以执行器回执为准。
```

- 三段齐全（`three_layers=true`），键名 `usable / unusable / how`；
- 这一段 654 字符 / 336 tokens，远低于脚本沿用的 4000 字符上限；
- 整轮请求 7155 tokens，上限 16000（`life/companion.py:425` `REQUEST_TOKEN_CEILING`），两种 purpose（dialogue/work）都在内；
- 另有一条独立通道：`tool_instructions_with_offline` 会把连接类掉线补成 instructions 里的一行
  （`integration: instructions_has_offline_block=true`、`instructions_offline_lines=["body：…"]`）。
  它的白名单是 `phone/computer/body`（`life/companion_context.py:47`），所以 phone 在 `available_actions`
  里时不出现——与 F-2 是同一个缺口的两面。
- 经验库场景（FakeIm 全关、无桌面手机）里：564 字符，`unusable` 前三条正好是 computer/phone/body，
  其余 7 条合并成一行指路。格式与设计一致。

---

## 5. C4 按需读取：模型被告知的读口逐个真跑

`GUIDES['memory']` 让模型在需要细节时用 `memory` 的扩展 op。这里走真执行器分派（`ActionExecutor.run` →
`companion_awareness.run`），不是直接调函数。

| 调用 | 状态 | 返回 | 说明 |
|---|---|---|---|
| `op=capabilities` | completed | returned 8 / available 16，`next_cursor=awareness:8` | 分页正确 |
| `op=capabilities, status=offline` | completed | 1 条，`action.body`，带原因 | 过滤后仍给原因 |
| `op=capabilities, limit=3` | completed | 3 条 + 游标 | |
| `op=capabilities, cursor=awareness:3` | completed | 8 条 + 游标 awareness:11 | |
| `op=awareness` | completed | 「会话 …；账号 connected；可用动作 8 种；…；能力 16（ready 10）」 | |
| `op=used, kind=[command,web]` | completed | 2 条 | **摘要句有问题，见 F-5** |
| `op=agent_tasks` / `op=attention` | completed | 空 + 明确的空语义 | |
| `op=experience` / `people` / `conversations` | not_implemented | 各带原因 | 语义不是「查无此事」 |

细节层：`ready` 条目 8/8 都带 `what` 与 `how_ref`；整目录 16 条 `capability_ref` 能一条条读出来
（`action.*` 12、`agent.subagent`、`channel.qq/wechat`、`guard.watchdog`）。
`guard.watchdog` 因隔离守卫拿不到 systemctl → `unknown`，原因写「这台机器上没有 systemd（或读不到它的状态），
所以我不知道有没有人替我兜底；不能断言有，也不能断言没有」——**这正是「不知道就说不知道」的正确形态**，
与被守卫拦下的 24 次 systemctl 调用互为佐证。

**F-5（新发现，中）`op=used` 的摘要句把「没做过」说成「用过 0 项：」。**
`life/companion_awareness.py:1085-1097`：

```
used = [item for item in items if item.get('counts', {}).get('total')]
never = [item for item in items if item['standing']['state'] == 'never_used']
text = '用过 ' + str(len(used)) + ' 项：' + '；'.join(lines)   # 空表也照写冒号
```

触发条件：**显式点名 kind**（`arguments={"kind":[...]}`）且这些 kind 在台账里没有动作。
不点名 kind 时走的是另一条 `if not items` 分支（说法正确）。实测两种写法同时出现在同一份返回里：

```
op=used, kind=[command,web] → 用过 0 项：。没做过的是：command、web（只是没做过）
op=used（不带 kind）        → 这段时间里台账没有任何动作。没做过不等于做不到；现在的可用状态看 capabilities。
```

最小复现：`ops/repro-used-never-empty.py`（云端同目录）。它在隔离 HOME 建空库直接调 `_used`，
输出 `DEFECT: YES`。这与验收规格 §4 第 3 条「空的就写没做过，不许用空数组冒充查无此事」
方向相反：**这里是把「没做过」写成了「用过」的句式。**

---

## 6. C5 经验记忆（a4 复现）

在同一进程里：真 `ActionExecutor` 分派 → 真 `Companion._settle` → 真 sqlite（隔离 HOME 里的新库）。

- 三件动作：`command` 成功、`web` 成功、`command` 失败（`sh: df: command not found`）。
- `memory op=used` 返回：`command` 2 次（成功 1/失败 1）`used_failed`、`web` 1 次 `used_ok`、`dsh` `never_used`；
  `basis=first_hand`（`dsh` 为 `reported` 类型，没做过时仍分开标）。
- 回查：`op=read` 用同一条 `action_id` 取回 `Filesystem /dev/vda3 39G 18%`，`evidence_scope=stored_output`，
  并且明确写着「不是你重新执行它的证据」。
- `_settle`（t3 靶点 A）生效：成功动作之后项目上有 `last_receipt`（`companion-action_0319ab…`）。
- 不建表：本次运行新增表 `[]`（此前一轮记为 `companion_context_evidence/…` 是 `_settle` 写证据表所致，
  不是 a4 建的；两轮对比见 §7）。
- 一个观察（不是失败）：多条测试动作落在**同一秒**时，`_stamp` 只到秒，`op=used` 的「最近一次 X」可能指向
  同一秒里另一条 `web` 回执；`standing` 本身仍按行序正确。真实使用不会有这种同秒密度。

边界如实说明：这一组用的是**全新空库**，不是生产库副本。原因是复现 a4 要往库里写动作，
在副本上做等于往「生产数据的副本」里掺测试数据，会污染后面按台账读的东西（C1 的 agent 证据）。
若需要在副本上做，建议单独再拷一份副本，别共用。

---

## 7. 过程记录：我自己这边出过的三个假问题（都已修）

写验收脚本时有三处**是我接线错，不是产品错**，记下来免得被当成缺陷：

1. **没调 `attach_core`**：服务模式下 `CompanionServer.start()` 对执行器调 `attach_core(core)`
   （`life/companion_remote.py:164-166`）。脚本不起服务，第一轮全忘，于是 `memory` 扩展 op 一律回
   「这一层读不到当前投影：执行器没有接到角色上」——那是接线没做。修法：外层 `FakeRemote.attach_core`
   转发给内层真执行器。
2. **生产库副本放在 `tmp/` 下**：`life_db` 是相对 HOME 的 `data/life.db`（`life/role_config.py` 的转述值），
   第一轮因此读的是新建空库，`agent.subagent` 被误报 `needs_setup`。修法：把副本放进
   `<sandbox>/home/data/life.db`。
3. **`_dsh_endpoint` 指向的是假执行器**：临时 HOME 里没有 DSH 入口，`action.dsh` 落 `needs_setup`。
   修法：`--prod-evidence` 下按生产事实给探测结果，并在报告里写明这是**从生产读取的事实**而非本地实测，
   生产这一项仍需有人当真机核验（本任务范围外）。

另：脚本里 `life.companion` 并不导出 `DECISION/GUIDES/READ_OPS/OP_NAMES/ActionExecutor`，这些都要从各自模块取。
若以后有测试用 `from life.companion import DECISION`，同样会炸。记一笔，不改产品代码。

---

## 8. 结论与建议改动（按判据排优先级）

**能力链路现在哪里是通的**：能力清单能自洽（C1）、概览三段格式与预算达标（C3）、
按需读取全通（C4）、经验层从真实动作→回执→按需回查闭环（C5/C6）。
a1 记的「第 5 环节：结果回查断」在 t3 靶点 A 之后**已经闭合**，这次用真 `_settle` + 真 `op=used` 验证到了。

**还剩的四处不一致**（都不大，但正对着「断开某终端她能否准确解释变化」这条验收）：

| 编号 | 严重度 | 位置 | 改法 |
|---|---|---|---|
| F-2 | 中 | `life/companion_awareness.py:437-453` | ①探测抛错 → `unknown` +「手机控制通道没回话」，不再断言「本机没有」；②「注册过但没有流」要有出口：`available_actions` 里出现 `phone` 但 `has_stream` 全为假时，仍留一条 `unknown`/`offline` 的旁注（或在 `what` 里明说「壳在、流不在」） |
| F-3 | 中 | `life/companion_awareness.py:196-212` | 快照抛异常时 `state='unknown'`，`reason` 用「连不上通道自己，拿不到登录状态」，**不要**再退回配置文件判 `disabled` |
| F-5 | 中 | `life/companion_awareness.py:1085-1097` | `used` 为空时不要写「用过 0 项：」；空表直接走「没做过」的句子 |
| F-1/F-4 | 低 | 摘要 `usable` 生成处（`:799-800`） | 动作与连接分两行/两类标出，或给连接条目加前缀，避免「能做的事」列表里混进通道；`needs_setup` 场景里 phone 的 ready 与 F-2 一并处理 |

以上四条**均未改动**：本任务只验证，不写产品代码，不写生产库，不发 QQ，不动服务。

---

## 9. 未覆盖 / 需要别人接的

1. `guard.watchdog` 的 ready/offline 只验了「拿不到 systemctl 时落 unknown」这一支；active/inactive 两支气管在隔离里被守卫挡住，
   仍以 t3 的真机实测为准。
2. `action.dsh` 的 ready 依赖「生产有 DSH 入口」这一事实，本脚本按 `--prod-evidence` 假设成立；
   没有在生产上真跑一次 dsh。
3. `computer/phone` 只验了状态与说明，没有派真动作（隔离环境本就没有真连接器）。
4. 行为侧（不提醒工具名问她能做什么、断开终端后她的解释）不在本任务范围，仍归 behavior-verifier。
5. 生产库副本本轮被删掉以省盘（380MB）；`verification.json` 里记着它的 sha256，需要时随时可重新拷。

---

## 10. 复验记录（append-only：不改上面任何一条结论）

2026-11-18 00:49–00:51，对 **t3-v3 已上线的生产代码**复跑。云端 md5（部署宣称与实测一致）：
`companion_awareness.py=dd66cc3f…`、`companion_agents.py=a2123c5e…`、`companion_context.py=6c6756e5…`；
`companion.py` 实为 `dc79056c…`（不是 `b63917d1…`，见 §10.4）。
证据：`/opt/qq-deepseek/.acceptance/20261118-005005-cap/`。

### 10.1 我改了自己的判据什么地方（先把改判据写清楚，再看结果）

| 条目 | 改前 | 改后 | 为什么 |
|---|---|---|---|
| C1 `doc_without_code` | 摘要里有、代码 kind 集合里没有 都算 | **只算「在 DECISION enum 里、但定义自相矛盾」的情况** | 通道/守护本来就不是动作 kind；把 `channel.qq` 算进来是分母取错（§2 已记 F-1 低危，这里仍不改代码，只改判据口径） |
| C1 摘要解析 | 按 `：` 切首段 | **按能力目录 `capability_ref` 反向匹配名字**，另出 `unparsed_summary_lines` / `summary_names_not_in_catalogue` / `catalogue_entries_missing_from_summary` | 摘要格式已改 `名字（处境）`；切分隔符会把格式变更变成假告警 |
| C2 `needs_setup.action.phone` | `offline` | `ready` + 断言 `dispatchable=false` 且原因里说不出断言词 | **我原来写错了**：同一情形另一条用例写 `ready`，两条自相矛盾；`status` 属于执行器 |
| C2 `phone_unavailable.action.phone` | `offline` | `unknown` + 断言原因里无断言词 | 探测抛错就是不知道；原判据把「读不到」当成了「没连上」 |
| C2 新增 `channel_snapshot_broken` | 无 | 断言 `source_scope=runtime_snapshot_failed`、reason 不含「关/未登录」 | 直接把 F-3 钉成用例 |
| F-4 判据 | 只能看 status | 断言 `dispatchable` 字段 | 落差不该由 status 表达 |

### 10.2 复验结果（全部绿）

```
C1 code_without_doc []                 doc_without_code []
     unparsed_summary_lines []         summary_names_not_in_catalogue []
     catalogue_entries_missing_from_summary []
C2 ready / offline / needs_setup / phone_registered_no_stream /
   phone_unavailable / channel_snapshot_broken / unknown    —— 7/7 all_ok
C3 666 字符 / 7243 tokens（dialogue）、7245（work），三段齐全，未超预算
C4 op=used 空表：这段时间里一项都没用过，没有任何真实回执。没做过的是：command、web（只是没做过，不等于做不到）
C5 全绿（含 read_back_same_source=true、last_receipt_written=true）
F-5 最小复现：DEFECT: no
隔离：临时 HOME、库副本前后 sha256 一致、26 次外部调用全被拦
```

逐条对照我上一轮报的问题：

- **F-5**：真机复现脚本已 `no`；摘要句改成「一项都没用过」并保留「没做过的是：…」。
- **F-3**：`channel_snapshot_broken` 用例通过——配置里写着 `enabled=false` 也**不再**退回去说「主动关的」，
  而是 `source_scope=runtime_snapshot_failed` + 「连不上通道自己，拿不到登录状态；这是不知道，不是不在线」。
- **F-2**：手机探测抛错 → `unknown` +「我读不到手机通道的状态（探测抛错 RuntimeError）……这不能说明它坏了，也不能说明它没有」。
- **F-4**：注册无流 → `status=ready`（执行器判断）+ `dispatchable=false` +「现在派活会被拒（需要 App 在前台）」。
- **F-1**：按上面口径降级为不算问题（通道非动作 kind），未改代码。

### 10.3 这轮**不在我判据里、但读代码看到的**两件事（给下一轮，没算红）

1. **`state_scope` 与 `source_scope` 在通道条目上是两回事**：`state_scope` 给的是
   `_channel_facts` 的 `state`（`connected/disabled/unknown`…，`awareness.py:731`），
   `source_scope` 给的才是来源档位（`runtime_snapshot / runtime_snapshot_failed / channel_config`）。
   我第一版新用例把两者混用，被自己的夹具抓出来——**不是代码问题**，但下游若有人只读
   `state_scope` 判断「这条是不知道还是被关了」，会读到 `unknown` 而丢掉「为什么不知道」。
   建议文档里写明分工，或用例里只看 `source_scope`。
2. **`phone_unavailable` 场景下 `action.phone` 带不出 `dispatchable`**：`dispatchable` 只在
   「在 `available_actions` 里」的分支赋值（`awareness.py:670-671`），掉线条目没有这个键。
   这不算错（掉线条目本来就不谈派活），但如果下游按 `dispatchable is False` 统一判断，
   缺键会被当成 `None`/未知。记一笔，不改判据。

### 10.4 基线漂移（继续记）

`companion.py` 在 t3-v3 部署声明里写的是 `b63917d1…`，我实测云端是 `dc79056c…`；
两者与我上一轮验的 `c4dc0610…` 又都不同。**本轮结论是在 `dc79056c…` 上跑出来的**。
建议部署口述里把「声明 md5」和「`md5sum` 实读」并列打印，避免再出现对不上的情况。

---

### 10.5 流程教训（captain 2026-11-18 00:5x 公开纠正，记在这里给下一个接手的人）

**「对话坏了」是误判，根因是一行调用参数名。** 00:11–00:50 期间 captain 反复报告生产对话不可用，
据此让行为验证（t6）多次无效重跑。实际原因是 RPC 调用写错了键名：

```bash
# 错的
{"method":"chat","params":{"text":"在吗"}}      → ValueError
# 对的
{"method":"chat","args":["在吗"]}               → 正常回答
```

`life/companion_remote.py` 的 `/rpc` 入口读的是 `m.get('args', [])`（部署版 `:265`），
**`params` 会被静默忽略并报参数错误**——没有文档、没有 schema、没有错误信息提示「你是不是想写 args」。

取下限教训（三条，都该写进以后的验收规范）：

1. **接口调不通，先怀疑调用方**。判「服务坏了」之前，必须先用一个**已知能成功的最小调用**证明
   调用格式正确；只凭一个自己写的请求报错，不能归因到被调方。今晚这条如果先做，能省掉 t6 的多轮重跑。
2. **能力验收与通道验收要分开做**。本次隔离验证不通 `/rpc`，只构造能力目录与请求体，所以
   完全没受这个误判影响——不是因为运气，是因为判据只读被测代码的内部事实，不经过任何
   可能写错的入口。**判据越靠近内部事实，越不会被入口问题污染。**
3. **键名类错误没有保护时，记进文档就是唯一保护**。`/rpc` 只有 `args` 一种接受形式，
   建议在 `docs/` 里补一行「`/rpc` 请求体 = `{method, args[]}`」；今天已出现两次「凭印象写调用」。

与本轮其它结论的关系：**§10.2 的全部结论不受影响**（没有一条经过 `/rpc`）；
受影响的只是「生产是否稳定」这个判断，而那件事本身不是本任务的判据。

### 10.6 部署体例（captain 采纳为强制规矩）

今天三个 `companion.py` 值无一对上（我实读 `c4dc0610…` → 声明 `b63917d1…` → 再实读 `dc79056c…`）。
此后任何人部署必须贴出：

```
DECLARED <md5>  ACTUAL <md5>  MATCH / MISMATCH <path>
```

`DECLARED` 取自打包/上传时本地文件，`ACTUAL` 取自部署机 `md5sum` 实读；两者不等即视为未部署成功，
不许在口述里只写一个值。本条起因见 §10.4。

---

## 附：证据文件清单（`/opt/qq-deepseek/.acceptance/20261118-002824-cap/`）

| 文件 | 内容 |
|---|---|
| `REPORT.md` | 本文件 |
| `verification.json` | 全部机器可判定结果（C1–C6、隔离自证、库副本指纹） |
| `report.txt` | 上表的人读版（`render_report.py` 生成） |
| `prod_facts.json` | 生产库副本只读盘点（动作按状态计数：completed 1003 / failed 165 / cancelled 10 / …；子 agent 任务 1 条 interrupted；tool_result 1170 条） |
| `verify-capability-chain.py` | 可复跑主脚本 |
| `repro-used-never-empty.py` | F-5 的最小复现 |
| `render_report.py` / `show_report.py` / `show_extra.py` / `prod_copy_facts.py` / `show_brief.py` / `show_c2_detail.py` | 报告与盘点工具 |
| `tmp/` | 隔离 HOME 与场景目录（不含 DB 副本；副本约 380MB，已删，指纹在 `verification.json`） |

复验目录：`/opt/qq-deepseek/.acceptance/20261118-005005-cap/`（§10 的全部数据；含修改后的脚本与 `summary.txt`）。

第二次（当前代码）复跑目录：`/opt/qq-deepseek/.acceptance/20261118-002925-cap/`
—— 同一套脚本 + 它自己的 `verification.json` / `report.txt` / `summary.txt` / `show_brief.py`，
并在 `report-002824.txt`、`verification-002824.json` 里保留第一轮的原始结果，便于逐条对照。
