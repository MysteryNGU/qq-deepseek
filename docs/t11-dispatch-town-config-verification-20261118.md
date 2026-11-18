# t11（P30）验证报告：真实派活 / town 判据 / config 路径

日期：2026-11-18 01:06–01:36 CST　执行：isolation-verifier
被验对象：云端部署字节（打包回本地解开，md5 逐文件对齐后才跑）

```
life/companion.py                 837513ab1d01b7e495d6fb69ca4648ff
life/companion_awareness.py       80308acd13b5ed4e4e612e9a828103ce
life/companion_agents.py          a2123c5e7d95f6a439063b65563ae578
life/companion_town.py            33bd56a1fb0bdaf35bc61a9f23228d62
life/role_config.py               6012ee71cb25b605d704e6027f8d501e
```
（captain 口述基线是 `awareness=21c420d7…`，实读已是 `80308acd…`——capability-engineer 的
t3-v4 修完 `dispatchable` 后又部署过一次。**按 §10.6 的规矩，本报告只认实读值**。）

---

## 第一部分：真实派活（端到端 + 卡死检测 + 台账不变式）

### 结论

| 层 | 判据 | 结果 |
|---|---|---|
| L1 派发 | worker 是否从 `pending` 领走了动作 | **通过**：0 秒 pending → 1 秒内台账出现行并推进 |
| L1 派发 | worker 是否**回过**子 agent 台账（用行状态变化断言） | **通过**（stub 路径 `running→completed`，work 路径 `running→failed`） |
| L2 执行 | 任务是否 `completed` | **stub：通过**；work：失败且原因如实（见下） |
| L3 核验 | `verification` 是否离开 pending | **stub：`passed`**；work：`failed`（与执行结论一致） |
| 卡死检测 | 60 秒内台账无变化即报「派发层可能又回不到台账」 | **一次都没有触发**（每条都在 1 秒内推进） |

**worker 死循环这个 bug，在我这份部署字节上已经修好了。** `_workers()` 现在是
`did = (_agent_pending_one) or (_agent_worker_one) or (_worker())`，`_worker()` 不再自带
`while True`；三条路共用一次「睡到唤醒」。实测三条路径都被领走：

```
stub: 0.0s action=pending  ledger=[]              → 1.0s action=queued ledger=[('completed','passed')]
work: 0.0s action=pending  ledger=[]              → 1.0s action=queued ledger=[('running','pending')]
                                                  → 2.0s ledger=[('failed','failed')]
dsh : 0.0s action=pending  ledger=[]              → 1.0s action=failed ledger=[]
```

### 三条后端各自「跑到了哪一层」（**不许合并成一个「成功」**）

**1. `node=stub`（离线后端）—— 全链路通，这是「派发层修好了」的硬证据**

- 交付物真实落地：`data/companion/agents/companion-agent_6fbd…/report.md`，22 字节，
  `changes[0].sha256=be86caf5…`；
- 结论 `outcome=completed`、`verification=passed`，能力目录同一时刻变成
  `agent.subagent status=ready dispatchable=true evidence=["companion_agent_tasks:…"]`。
  **`ready` 只在台账里真有一条 completed 时才出现——这条判据被真正触发了，不是靠推理。**

**2. `node=work` —— 派发层通，执行层失败，而且失败原因如实**

```
status=failed verification=failed
failure={"code":"nonzero_exit","reason":"退出码 2"}
result ={"summary":"command backend needs a command\r\n", ...}
```
原因不是 mystery：`companion_agents.py:_run_worker()` 用 **`script=str(action.get('command'))`**
把活交给命令后端（`companion_agent_worker.py:run_command`），而 `create` 只写了 `goal`。
**换句话说：`node=work` 的子 agent 必须带 `command`，写 `goal` 的活一定失败。**
这不是我这轮才引入的——是产品既有语义（`life/companion_agents.py:1956-1972`），
但**它没有出现在任何给模型看的说明里**：`AGENT_INSTRUCTIONS` 写的是
「create 要 goal（做什么、在哪做、怎样算做完）……node（dsh/work/echo）」，
按说明去派 `node=work` + 只有 goal 的活，必然拿到 `command backend needs a command`。
这正是 t5 那类「说明与实现不一致」，见 §4 建议。

**3. `node=dsh` —— 派发层通（动作被领走并落 `failed`），执行层在容器里无法验**

```
action status=failed
result={"status":"failed","code":"dsh_unavailable",
        "reason":"这台机器上找不到 DSH 运行时（dsh_root/dsh_node/入口缺失），换 node=work，或者先把 DSH 装好。"}
```
容器里没有 DSH 运行时，所以**不产生台账行**是正确行为（在 `AgentManager` 层面就被拒了，
根本没到 `queued`）。生产上 `dsh_root` 是否有值，这轮**不在我的判据里**，见 §4 未覆盖项。

### 「能力接通」与「那件活的成果核验」分开报

- `agent.subagent` 的 `ready` = 台账里有过一次 completed（**能力接通**）；
- `verification` 是**那一次活**的成果有没有被证据支持。stub 那次 `passed`，work 那次 `failed`。
- 生产上 captain 看到的 `completed + verification=pending` 属于第三种情况：**跑完了，但没留
  可判定的交付物证据**——`pending` 不转 `passed` 是对的，不是 bug。这一条我在 01:0x 已经说过，
  本轮实测再次印证：stub 留下 `changes[0].sha256` 就 `passed`，work 没有交付物就 `failed`。

### 怎么复跑

```
# 云端打包（root）
tar -czf /root/.t5snap/life.tgz -C /opt/qq-deepseek --exclude=__pycache__ life
# 取回解开到 <container>/life，<container>/ 下补 supervise.py
PYTHONPATH=<container> python -B -X utf8 scripts/verify-agent-dispatch-worker.py \
    --out <outdir> --stall 60 --poll 1
```

---

## 第二部分：问题 A —— `action.town` 判据说瞎话

### 结论：**确认是真缺陷，而且是「写死的一句话」，不是判断失误**

**⚠ 数据更正（captain 2026-11-18 01:4x 指出，复查后确认是我写错）**：
我把 `town.json` 与 `town-token` 两个文件混为一谈。实读：

```
town.json      16 字节  {"enabled":true}            keys=['enabled']
town-token     38 字节  fbo_3cGG2B2MF98qyvaPeunYGw6XhybMNWNmTW   mode=600
TownConfig.load -> enabled=True, bool(code)=True
```

即 **`enabled` 在 `town.json` 里、接入码在独立文件 `town-token` 里**，两者都齐。
`TownConfig.load()` 读完两者后 `bool(code)=True`——所以「有接入码」这个结论是对的，
但**我给出的出处写错了**（说成「town.json 里 token 38 字」）。这类「结论对、出处错」比结论错更难抓，
因为它在下游引用时会被当成已核实的细节。已改。

生产实测（以 `companion` 身份跑）：

```
town.json  -> {"enabled": true}                    （16 字节，只有 enabled 一个键）
town-token -> fbo_3cGG2B2MF98qyvaPeunYGw6XhybMNWNmTW（38 字节，mode 600）
TownConfig.load() -> enabled=True, bool(code)=True
台账里 town 动作 199 条，最近一条 2026-11-18 00:39:44 completed
```

同一时刻能力目录：

```
action.town  status = needs_setup
reason = 小镇没启用或没有保存接入码（town.config.enabled=false 或 code 为空）；这是配置项，不是网络问题。
```

而 `_offline_reason('town')` 的**源码里根本没有读任何状态**：

```
mentions TownConfig: False
mentions town.config: True     ← 只出现在那句文案里
```

确切位置：`life/companion_awareness.py:502-504`

```python
if kind == 'town':
    return ('小镇没启用或没有保存接入码（town.config.enabled=false 或 code 为空）；'
            '这是配置项，不是网络问题。'), 'needs_setup'
```

**影响范围**（我实测了三种情形）：

| 情形 | 谁会被这条错文案骗到 |
|---|---|
| town 在 `available_actions` 里（生产现状） | **不会显示这条**——目录显示 `ready`。所以这份错文案平时被掩盖着，只有在 town 掉出清单时才会「正好」说出一个**未经验证的断言** |
| town 不在清单里、配置其实好好的（本节就是） | **模型会被明确误导**：配置没问题，却说「没启用或没接入码」 |
| 配置真坏了（`enabled=false` 或没有 code） | 文案碰巧对，但仍然是猜的——一旦有别的故障（网络、token 过期），文案照样说「配置项，不是网络问题」，**把真因挡住** |

生产上 `available_actions` 是否含 town，取决于 `companion.py:1522`
`if self.town.config.enabled and self.town.config.code: kinds.append('town')`——**判定在 `_messages` 里做，在 `_offline_reason` 里没有做**。两处不知道彼此，于是出现「一处知、一处不知」。

### 正确判据应该读什么（不改代码，给 capability-engineer 的修法）

`_offline_reason(core, 'town')` 应当与 `companion.py:1522` 用**同一个事实源**，并把三种情况分开：

```python
# 建议形状（不落地，交给实现者）
if kind == 'town':
    status, failure = _probe(lambda: core.town.snapshot())     # 复用已有的快照
    if failure:
        return _unknown_reason('小镇通道', '探测抛错 %s' % failure), 'unknown'
    if not status.get('enabled'):
        return '小镇在设置里是关着的（town.json enabled=false），不是网络问题。', 'needs_setup'
    if not status.get('has_code'):
        return '小镇开着，但还没保存接入码；需要在设置里把 /join 拿到的码填进去。', 'needs_setup'
    # 配置齐全 → 说得出「现在为什么不在这轮清单里」，而不是断言配置错
    return ('小镇配置是齐的（enabled=true、有接入码），这一轮它没被列进可用动作；'
            '要看实时状况用 town_connection。'), 'unknown'
```

要点：
1. **配置齐全时不许断言「配置有问题」**——改说「配置齐，但这轮没列」，并指向
   `town_connection`（`companion.py:1535` 已经在 `state` 里给了）；
2. 文案里那句「这是配置项，不是网络问题」**只有在真读了 `enabled`/`has_code` 之后才允许出现**；
3. `town_connection` 已经有 `enabled/has_code/state/error`，直接用，不必新读文件。

### 同类要一起查的

`dsh` 那条（`companion_awareness.py:507`）也是写死的「本机没有可用的 DSH 运行时」，
虽然 `capabilities()` 里对 dsh 有真实探测（`_dsh_endpoint`），但 `_offline_reason` 这个
函数被 `offline_reasons()` / 摘要 / 掉线条目**共用**，一旦走到这里就是断言。
`bilibili`（`:505`）同理。**建议：凡是 `_offline_reason` 里没做探测的分支，文案一律降级成
「这轮没拿到可用状态」并给 `unknown`**，把「具体原因」留给真探测过的 kind。

---

## 第三部分：问题 B —— 生产 config.toml 里的 Windows 路径

### 结论：**不影响正常运行；只是「静默用不到」，不是崩**

实测（`companion` 身份，真 `load_config` 生产配置）：

```
config.toml has bili_plugin line: True
effective cfg.bili_plugin = 'C:\\Users\\GEUEK\\.dsh\\plugins\\dsh-bili-subagent'
is_absolute = False                              ← Linux 上反斜杠不是分隔符，整串被当成一个相对文件名
primary source  exists = False
fallback source exists = True   → /var/lib/companion/xiaoyu/data/companion/bilibili/mcp_server.py
=> Bilibili.available() = True                   ← 走的是回退那份
```

逐条判定：

| 问题 | 结论 | 证据 |
|---|---|---|
| 会不会让服务起不来 | **不会** | 服务 active、`NRestarts=0`；`load_config` 不 stat 这个路径 |
| 会不会让 B 站能力消失 | **不会** | `Bilibili.available()` 因为回退目录里有 `mcp_server.py` 而返回 True（实测），所以 `bilibili` 仍在 `available_actions` 里 |
| 会不会「炸」 | 不会**抛异常**，只会**静默走回退** | `Path("C:\\Users\\…").is_absolute()` 在 Linux 上是 False，`is_file()` 返回 False（ENOENT，不是 EACCES），于是代码落到 `self.folder/'mcp_server.py'` |
| 那 t6 报的 PermissionError 是什么 | **是他自己的采样脚本**对这条路径做了 `stat`/遍历，不是产品代码 | 我用同一路径 `stat()` 得到的是 `FileNotFoundError`；产品代码路径（`companion_bilibili.py:21-23`）不会抛 |
| 会不会影响子角色派生 | **会**（值得改） | `character_defaults.py:25-26` 对 `bili_plugin` 做 `(source/cfg[key]).resolve()`——Windows 串在 Linux 上会被解析成 `/var/lib/companion/xiaoyu/C:\Users\…`，子角色继承一个**更假的路径** |

### 需要改配置还是改代码

**两个都该做，但性质不同：**

1. **改配置（运维动作）**：生产 `config.toml` 里的 `bili_plugin` 应该删掉或写成 Linux 路径。
   删掉最干净——`role_config.py:58` 的默认值就是 `Path.home()/'.dsh/plugins/dsh-bili-subagent'`
   （companion 用户的 home 是 `/var/lib/companion`），回退逻辑照样能工作。
2. **改代码（最小修法，给实现者）**：`companion_bilibili.py:21-23` 现在**不区分「路径不存在」
   和「路径写错/跨平台」**，两者都静默回退。建议：

```python
package = Path(getattr(cfg, 'bili_plugin', '') or '')
configured_but_missing = bool(package) and not package.is_dir()
if configured_but_missing:
    # 记一次，不抛：配了却用不上，是运维问题，不是运行时错误
    log.warning('bili_plugin 配置了但在这台机器上不存在：%r（回退到 %s）',
                str(package), self.folder)
```

   **不要把它改成抛异常**——那会让一个配置笔误直接变成「服务起不来」。
   同理 `character_defaults.py:25-26` 建议对 `is_absolute()` 或盘符形状做一次判断，
   避免把 Windows 路径 `resolve()` 成本机更离谱的路径再写进子角色。

---

## 第四部分：报告 captain 的两件额外发现

### 4.1 硬伤：`node=work` 的 `command` 语义从未写进给模型看的任何地方

**事实链（部署字节，逐处可查）**：

| 环节 | 代码 | 说了什么 |
|---|---|---|
| 给模型看的说明 | `life/companion.py:330-342` `AGENT_INSTRUCTIONS` | 「create 要 **goal**（做什么、在哪做、怎样算做完）、node（dsh/work/echo）…」 |
| 模型能填的字段 | `_action_fields` + `OP_FIELDS['create']` | `goal` 可填；**`command` 不在 `OP_FIELDS['create']` 里** |
| 实际执行取什么 | `life/companion_agents.py:1972` `_run_worker` | `script=str(action.get('command') or '')` |
| 没有 command 时 | `life/companion_agent_worker.py:72-75` | 打印 `command backend needs a command`，`return_code=2` → 台账 `failed` |

**结论：按说明派 `node=work` + 只有 goal 的活，100% 失败。** 而且它是**语法通过、执行失败**，
比 `inspect{agent_id}` 那种「被 schema 当场拒」的更隐蔽：模型拿不到「你写错了」的信号，
只会觉得「这件事做不成」。

**实测证据**：第一部分 work 后端那一次，`verification=failed`，原因就是这一句。
`node=echo`（= `stub`）同一条 goal 却能跑通 —— 差别只在「有没有 command 字段」。

#### 方案 A：改说明（不动行为）

在 `AGENT_INSTRUCTIONS` 与 `action.agent` 条目里写明：
> `node=work` 必须同时给 `command`（一段可执行命令，在 workspace 里跑）；只给 goal 的活请用 `node=echo`。

但**光改说明还不够**：`command` 现在**不在 `OP_FIELDS['create']` 里**，说明里让模型写，
`_action_fields` 会在入口把它丢掉（t3 靶点 E 的做法相反）。所以方案 A 实际是两处联动：
1. `OP_FIELDS['create']` 加 `'command'`（目前是白名单，多加一个键风险很低）；
2. `AGENT_INSTRUCTIONS` + `capabilities()` 的 `agent` 条目写清「work 要 command，echo 用 goal」。

| | 评价 |
|---|---|
| 改动量 | 小（两处常量） |
| 风险 | 低：不改变已有成功路径（现在带 command 的调用本来就工作） |
| 对模型的要求 | **高**：她得自己判断「这件活是命令还是自然语言」，判断错就失败。今晚 A2 的表现说明这类判断她并不总对 |
| 失败形态 | 仍会失败，只是失败前有说明兜着 |

#### 方案 B：改行为（`_run_worker` 兜底）

`_run_worker` 里 `script=str(action.get('command') or action.get('goal') or '')`。

| | 评价 |
|---|---|
| 改动量 | 更小（一个 `or`） |
| 风险 | **中**：`goal` 是自然语言（「把报告写成 markdown 并检查格式」），把自然语言当命令跑，
要么 shell 报 `command not found`（失败原因变得莫名其妙），要么更糟——**恰好是个能执行的词**，
比如 goal = `ls`，就真的在 workspace 里 `ls` 了。**这是「把意图当命令」的越权风险** |
| 对模型的要求 | 低 |
| 失败形态 | 从「明确的缺 command」变成「看起来跑了但没做对的事」，**更难诊断** |

#### 方案 C（我推荐）：拒绝得更早、更清楚，并把 `command` 暴露出去

`create()` 里对 `node=work` 做一次前置校验：

```python
origin = ORIGINS.get(str(node or '').strip().lower())
if origin == 'work' and not str(action_command).strip():
    raise AgentError('command_required_for_work_node',
        'node=work 要在 arguments 里给 command（一段可执行命令）；'
        '如果这件事是「写文件/做产出」，用 node=echo 并把产出写成 goal 里的脚本。')
```

同时把 `command` 加进 `OP_FIELDS['create']` 与 `AGENT_INSTRUCTIONS`。

| | 评价 |
|---|---|
| 改动量 | 小到中（一处校验 + 两个常量） |
| 风险 | **最低**：不改变任何语义，只把「必然失败」提前成「明确拒绝 + 说清怎么改」 |
| 对模型的要求 | 中，但**失败即得反馈**（与 `inspect{agent_id}` 被拒同型——那类她学得会） |
| 与现有架构一致 | 是：`create()` 里已经有 `goal_required`、`workspace_outside`、`nesting_limit` 同类前置拒绝 |

**我的取舍建议**：**C + A**（C 兜底保证不再静默失败，A 让说明与 OPT_FIELDS 对齐）。
**不推荐 B**——「自然语言当命令跑」的越权面比它解决的问题大。
如果只做一件事，做 C 的前置校验：它把「必失败」变成「当场说清缺什么」，
而这正是今晚反复出现的主题（失败要说得出下一步做什么）。

#### 顺带：为什么这个缺陷能活到现在

`test_agent_repair` 等用例走的是 `node=echo/stub`（goal 即脚本），`node=work` 的真实语义
（跑 command）从来没有被一条「只有 goal」的用例覆盖过。**建议补一条回归**：
`create(node='work', goal=...)` 不带 command → 必须得到 `command_required_for_work_node`，
而不是 2 秒后从台账里读出一个 `command backend needs a command`。

### 4.3 输入/插话循环（需求 1、2）
本轮为验派活只读了 `_workers()` 一个循环，**没有**去动 `_input_batch` / 气泡调度那条链，
所以对「插话打断」「收回未发气泡」这两条**没有观测证据**，不发表结论。若下一步要验，
我的判据会是：**用事件/台账的状态变化断言「她的决定」被记录进了哪张表**，而不是看她说了什么。

### 4.2 「沉默断言」：判断依据必须来自程序，不能写在文案里（captain 提升为通用规则）

captain 把「被掩盖的未经验证断言」归纳为一类系统性缺陷，我按那个口径把 `_offline_reason`
的每个分支逐一核对了一遍。**判定口径**：这个分支有没有在**当前这一轮**真的读到状态，
还是把一句结论直接写进字符串。

| kind | 位置（`life/companion_awareness.py`） | 是否真探测 | 断言内容 | 判定 |
|---|---|---|---|---|
| `phone` | :472-489 | ✅ `_probe(executor.phone_context)` | — | 合规（t3 三轮修过） |
| `computer` | :490-496 | ⚠ **半探测**：`_probe` 只探「执行器链上有没有 peer」，**没探「peer 是不是真的活着」** | 「桌面端没有连上（客户端 websocket 没连进来）」 | **半合规**：失败分支会 `unknown`，但 `peer is None` 时仍是断言式结论 |
| `body` | :497-501 | ⚠ 同 computer：`_probe(core._body_context.connected)` | 「动作发过去没人接收」 | 半合规 |
| **`town`** | **:502-504** | ❌ **完全不读**（实读源码：`mentions TownConfig: False`） | 「没启用或没有保存接入码」「这是配置项，不是网络问题」 | **违规** |
| **`bilibili`** | **:505-506** | ❌ 不读（`Bilibili.status()` 里其实有 `available/credential_present/connected`，**现成的**） | 「没有登录态（插件未配置或登录已失效）」 | **违规** |
| **`dsh`** | **:507-508** | ❌ 不读（`capabilities()` 里有 `_dsh_endpoint()`，`_offline_reason` 里没有） | 「本机没有可用的 DSH 运行时」 | **违规** |
| 其它 | :509 | — | 「执行器没有把它报成可用；具体原因我这边看不到」+`unknown` | 合规（这正是该有的废物措辞） |

**必须补探测的三个（按危害排序）**：

1. **`town`** —— 最严重，因为它是**唯一一个被自己误导过的**：台账 199 条成功动作在旁，
   文案却说「没配置」。现有事实源：`core.town.snapshot()`（`enabled/has_code/state/error/turns`）。
2. **`dsh`** —— 现成事实源：`_dsh_endpoint(executor)`（`capabilities()` 已在用），
   拿到 `None/False/True` 三态即可区分「没有运行时」与「没探测」。
3. **`bilibili`** —— 现成事实源：`Bilibili.status()`（`available / credential_present / connected`），
   三者是三种不同的「没登录」：**插件没装 ≠ 没扫码 ≠ 掉线**，现在被一句话抹平了。

**通用规则（建议写进 docs，给以后所有 `_offline_reason` 新增分支的人）**：

> **`_offline_reason` 的每一条返回值，要么在本次调用里真的读到了那个对象的状态，要么就只能说
> 「我这边没拿到它的状态」并给 `unknown`。禁止把「配置没配」「没登录」「没有运行时」这类结论
> 直接写进字符串——那些是**判断**，必须由程序在本次调用里做出来。**
>
> 自查方法（可机器判定）：对每个 kind，把 `_offline_reason(code, kind)` 在一个**状态正常**的
> 夹具上跑一次；若返回 `needs_setup/offline` 而不是 `unknown`/`ready`，说明它没有探测，
> 只是写死了一句话。我这一节就是用这个办法逐条过的。

**半合规两个（computer/body）的补充说明**：它们的 `_probe` 只探「对象在不在」，不探「连没连」。
`computer` 的真实连通是 `server.peer` 是否活着（`available_destinations()` 用同一个 `peer is not None` 判定），
`body` 是 `_body_context.connected`——两个都不带时间戳，**没法回答「什么时候看的」**。
建议让它们与 `phone` 对齐：返回 `{connected, observed_at}`，文案里带上观测时刻。

---

## 第五部分：未覆盖 / 需要别人接的

1. **`node=dsh` 的真实执行没验**：容器无 DSH 运行时。生产上 `dsh_root` 是否配置、能否真跑一次
   dsh 子 agent，需要有人在生产上派一次（这会花模型钱，我没擅自做）。
2. **worker 死循环只在修复后的字节上验过**：我没有在修复前的字节上复现「永远停在 pending」，
   所以「这是同一个 bug」的因果链来自 captain 的描述 + 代码对照，不是我亲手复现的。
3. **`node=work` 失败**是产品语义，不是缺陷，但**需要决策**「改说明还是改行为」，我给了两个方案。
4. **生产 `verification=pending` 那条记录**（`companion-agent_f9c833…`）我没去追它为什么没
   `passed`；按判据它属于「跑完但没留证据」，若要追需要看它当时的 `changes`/`artifacts`。

---

## 附：证据清单

| 文件 | 内容 |
|---|---|
| `.acceptance-local/t11-worker-final/agent-dispatch-worker.json` | 三条后端的逐秒时间线、信号、能力目录、`op=used` 输出 |
| `scripts/verify-agent-dispatch-worker.py` | 可复跑脚本（走真 `_workers()`，不是直调 API） |
| `scripts/verify-agent-dispatch.py` | 第一版（直调 API），保留作对照，说明「直调 API ≠ 产品路径」 |
| `/root/.t5snap/life.tgz`（云端） | 被验的部署字节；本地解开后 md5 与云端 `md5sum` 一致 |
| `/tmp/t11ev/verify_town.py`、`verify_bili_path.py`、`bili_consumers.py`（云端） | part 2 / part 3 的可复跑探针（只读，`companion` 身份跑） |

本轮**未改任何产品代码、未写生产库、未发 QQ、未重启服务**；所有云端脚本只读。
