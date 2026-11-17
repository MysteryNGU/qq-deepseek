# t5 rev-3 复验：`dispatchable` / `state_source` 契约（本地，production 容器）

日期：2026-11-18 01:00–01:02 CST　执行：isolation-verifier
起因：capability-engineer 部署 t3-v4（`life/companion_awareness.py=21c420d7…`），
修了我在 §10.3 提的两条遗留风险，并请我判断「要不要为 `dispatchable` 补判据」。

**结论：全绿。** C2 七场景全部 `all_ok=true`，每个场景额外 5 条契约检查也全部 `true`。
本轮**没有**连接云端跑（原因见 §1），但在**真实部署容器**里跑，可信度与线上等同。

---

## 1. 为什么在本地跑、它在多大程度上等价于线上

这一轮的改法动了「每条能力条目都带不带 `dispatchable`」这种**全目录口径**，
而 C1/`op=capabilities`/摘要预算恰恰会读到全部条目。校验它需要连真 executor + 真 sqlite，
光拷一份 `companion_awareness.py` 是验不了的。做法：

1. 把**云端部署目录整份**打包（`tar -czf /root/.t5snap/life.tgz -C /opt/qq-deepseek life`），
   scp 回本地解开；逐文件 md5 与云端 `md5sum` 一致
   （`companion_awareness.py=21c420d7…`、`companion.py=dc79056c…`）；
2. harness 原来放在 `ROOT/ops/`，靠 `parents[1]` 定位仓库根；这次放进
   `<container>/scripts/`，`parents[1]` 正好是容器根，于是 import 的就是**解开的那份部署代码**，
   而不是我工作区里的本地副本；
3. 容器缺 `supervise.py`（`life/runtime.py` 顶层 import），从工作区补了一份进容器
   ——**这一份是工作区的，不是云端的**；它在验收里只提供 `validate_data_compatibility`，
   不参与能力目录，但要如实标注这处不等价。

时效提示：云端 `.t5snap` 打包时写在 `/var/lib/companion/xiaoyu/data/.t5snap/`，
那个目录**在我打包之后约一分钟内就被删了**（该路径受 `supervise`/cleanup 管），
所以最终改到 `/root/.t5snap/` 重打一次。**以后不要在 `data/` 下放打包临时目录**，
这次差点白做。

与「云端隔离快照」相比唯一的区别：**没有加载生产库副本**（`--prod-evidence` 未开，
`c1_basis=fresh sandbox home`）。因此 `agent.subagent` / `action.dsh` 的 ready 不由生产台账决定；
这一项上一轮（§10.2，`-005005-cap`）已经在生产库副本上验过，且本轮代码没动这两条路径。

## 2. 跑法（可复跑）

```
# 1) 在部署机打包（root）
tar -czf /root/.t5snap/life.tgz -C /opt/qq-deepseek --exclude='__pycache__' life
# 2) 取回本地
scp root@host:/root/.t5snap/life.tgz <container>/   && tar -xzf life.tgz
# 3) 把 harness 放进 <container>/scripts/，<container>/ 下补 supervise.py
PYTHONPATH=<container> python -B -X utf8 scripts/verify-capability-chain.py --out <outdir>
```

结果文件：`.acceptance-local/run-20261118-0100-cap/verification.json`（本地）。
**注：脚本本轮加了契约检查，云端旧目录（`-005005-cap` 等）里的脚本副本是旧版；
下一次云端跑之前需把新脚本再传一次。**

## 3. 新增的 5 条契约判据（每个场景都跑，不只点名的那几条）

| 检查 | 判据 | 本轮结果 |
|---|---|---|
| `dispatchable_key_on_every_entry` | 目录里**每条**都有 `dispatchable` 键 | `[]`（没有漏） |
| `dispatchable_boolean_or_none` | 值只能是 `True/False/None` | `[]` |
| `dispatchable_false_has_reason` | `ready` 却 `dispatchable=false` 的条目必须给原因 | `[]`（手机那条带 caveat） |
| `state_source_on_channels` | 每个 `channel.*` 都有 `state_source` | `[]` |
| `summary_budget_chars` | 摘要这一段 ≤ 4000 字符 | 646–666 |

这五条覆盖了上一轮我手工发现的两件事：**掉线条目缺键**、**通道丢来源档位**。
现在它们是机器判据，不再是「§10.3 里记了一笔」。

## 4. 顺手验到他改的措辞

`needs_setup` 场景（手机 0 设备，执行器却报可用）的 reason 已变成
「执行器把手机报成了可用，但我这边看不到任何在线设备」——与夹具事实一致，
不再照抄「注册过但没有活动连接」。`phone_registered_no_stream` 场景仍是
「注册过…没有活动连接…派活会被拒（需要 App 在前台）」，两条话术现在各说各的情形。

## 5. 其它组（同一次运行）

```
C1 code_without_doc []   doc_without_code []   未解析行 0   摘要/目录互缺 0
C3 dialogue 666 字符 / 7274 tokens，work 666 / 7276，三段齐全
C4 used 空表：「这段时间里一项都没用过，没有任何真实回执。没做过的是：…」（F-5 仍为 no）
C5 read_back_same_source=true  last_receipt_written=true  未新建表
隔离守卫拦下 26 次外部调用（watchdog 探测 systemctl），库/QQ 均未触碰
```

## 6. 遗留（继续挂在 t7 评审清单）

1. `state_scope` 与 `state_source` 并存：前者是通道状态本身，后者是来源档位。
   下游**必须读 `state_source`** 才能分辨「不知道」与「被关掉」。这个分工靠注释维持，
   没有 schema 强制。
2. `dispatchable=None`（通道、守护）与「不在清单里 → `False`」语义相邻，
   写下游判断时容易把 `None` 当成 falsy 一锅端。
3. 本轮未开 `--prod-evidence`，`agent.subagent`/`action.dsh` 的 ready 判据以
   `-005005-cap` 那次生产库副本的结果为准，未在本轮重测。

---

## 7. 补丁（captain 2026-11-18 01:0x 指出，append-only）

**本轮「全绿」的适用范围要收窄一句：它证明的是「代码逻辑对」，不是「这条路径真的跑过」。**

capability-engineer 的 `_workers()` 里有一个「一旦进入 `_worker()` 就再也不回来看子 agent 台账」的循环
（`_worker()` 内部 `while True` + `_wake.wait()`，永不返回），导致 `kind=agent` 的动作**永远停在 pending**。
captain 发现并修好后，生产上才第一次出现 `companion-agent_… | completed`（01:01:34 派活 / 01:01:56 完成）。

影响本报告哪几条：

| 本报告的结论 | 是否受影响 | 说明 |
|---|---|---|
| C1 `agent_explained_by`（说明在 `AGENT_INSTRUCTIONS` 里） | 不受影响 | 只看模板文本是否存在，不依赖运行 |
| `action.phone` / `channel.*` / `guard.watchdog` 全部状态判据 | 不受影响 | 这些路径真在跑（手机注册、通道快照、systemctl 都在真实调用链上） |
| **`agent.subagent` 的 `needs_setup` / `ready` 判据** | **受影响** | 判据本身没写错，但它在**真实派活路径上从未被走到**——坑在派发层，不在能力判定层 |

**这是「代码有、路径没跑过」的典型**：`capabilities()` 对 `agent.subagent` 的判定只读台账
（`companion_agent_tasks` 有没有一条 completed），台账里一条都写不进去时，
它读到的永远是「没有证据 → needs_setup」，而这个结论**恰好是对的、也恰好掩盖了 bug**——
一个永远派不出去的能力，看起来就像「还没跑通过」。**判据如果是「用证据决定状态」，就必须同时验「证据能不能被写进去」。**

### 下一轮（captain 已约定）要验的东西与上一轮不同

captain 已明确：修完 worker bug 后的复验**要验「真实派活路径」，不是「代码逻辑」**。下一轮至少要有：

1. **端到端一次真派活**：`kind=agent` 走 `create` → 台账出现 running → 落 completed → `op=used` 能读到
   `reported` 那条、`basis=reported` 而不是 `first_hand`；
2. **卡死检测**：派出去之后 60 秒内台账没有状态变化，就要报「派发层可能又回不到台账」，而不是等结果；
3. **`_workers()` 的顺序不变式**：用可观察的副作用（台账行状态变化）断言 worker 循环**确实回过台账**，
   而不是只看 `_worker()` 的代码形状。

### 关于 `dispatchable=None`：维持现状

captain 建议不改 `'na'`，理由是 `None` 让「不适用」与「不可用」**在类型上**就分开，改成字符串反而和
`True/False` 混在一起。我同意，撤回 §6 第 2 条里「要不要换显式词」这个提法——
**它本来就不是问题，是设计**。留下的只有「读的人要记住 `None` 不是 falsy 语义」这一条文档义务。
