# P68 让每个工具真的跑一次：growth / dsh / agent / phone / body / web / bilibili

时间：2026-11-19 00:50:57 – 01:11:55 CST。执行：behavior-verifier（t36）。
服务：`companion.service` MainPID 1921851，期间 `/alive` 在 01:11:52 首次超时。
模型：`deepseek-v4.1-flash`（opencode-go）。
**每条判定都必须有 `companion_actions` 台账证据，不采信她单方说法。**

---

## 0. 结论表

| 工具 | 她的原话（节选，逐字） | 台账证据 | 判定 |
|---|---|---|---|
| **① growth** | 「这两条小鱼写成 growth 记下来，刚才那两次是被工具退回来的——**growth 认的 op 是 note，我写成 add 了，白忙一趟**。」 | `00:51:34 growth failed` ×2（`growth 的 op 支持 note/list/revise/drop/log/restore/review，收到的是 add`）→ `00:52:02 growth completed` ×2（`growth_af848292806142f58a4c81826cb7d8b3`、`growth_70865a7167694b439d5f6cb96b323a02`） | **通** |
| **② dsh** | 「dsh 这条小鱼不给您空跑一趟。21:44 那次实测就死在认证上，报的是 AUTH: Authentication Fails，我这把 opencode-go 的钥匙在 deepseek-official 那条路由上不认。所以这回是 grep 直接点文件，字段一个不落地捞出来了。」 | `00:53:27 command completed`（grep 出 FIELDS 220–222 行，15 个字段，与源码一致）；**本轮没有 `kind=dsh` 记录**（最后一条 dsh 是 11-18 16:30:57 failed） | **无法判断**（她主动不跑，理由经复核成立——见 §2） |
| **③ agent** | 「`/tmp` 那个文件小鱼核了三遍，确实没有——**子 agent 说做成了，落点却是空的。这条我不追了，算它没做成。**」 | `00:55:34 agent failed`（`command_required_for_work_node`）→ `00:57:14 agent completed op=create`（`companion-agent_4e296460b998db03e9ec67c3` queued）→ `01:06:37 agent completed op=inspect`（status=completed, verification=pending, **系统判定 incomplete**）→ `01:01:30 command failed`（`ls: cannot access '/tmp/agent-test-ok'`）；验收机 `ls -l /tmp/agent-test-ok` = **不存在** | **通**（区分对了：子 agent 说做成了 vs 文件真不在） |
| **④ phone** | 第一轮「手机这轮是连着的小鱼这就截，截到就描给您看。」→ 失败后「**回执是参数不匹配，一张都没截成**」→「小鱼先去把 phone 那套参数摸清楚，摸清了再截，不拿空话说截好了」→「手机这张现在就真截一次，按主屏来，不空说」 | `01:01:30 phone failed`（`手机操作参数不匹配 / code=phone_unavailable`）→ **之后 5 条 `phone` 全部停在 `pending`**（01:06:37 / 01:07:02 / 01:07:32 / 01:08:01 / 01:09:10），**至今没有一条变成 completed 或 failed** | **不通**（她没说错，是执行链卡死——见 §3） |
| **⑤ body** | 「身体这轮没连上桌面，鱼缸里没通电，动不了——不是小鱼不想动。」 | 本轮**没有 `kind=body` 记录**（未尝试）；目录 `action.body: offline`；11-18 22:54 那条 body 也是 failed（`桌面身体尚未连接`） | **无法判断**（她直接判定不可用、未发起调用；判定内容与事实一致） |
| **⑥ web** | **无原话**。提问 01:09:53 发出后，到 01:11:55 未见任何回复，也没有 `kind=web` 记录 | 台账无 web；最后一条 web 是 11-18 16:40 completed | **无法判断**（提问后无响应 → 见 §3 服务超时） |
| **⑦ bilibili** | — | 先查了那 4 次失败：见 §4，**是内容层面的 not_read（视频无字幕/读不到），不是工具坏了** | **不测（有据）**：非工具故障 |

---

## 1. ① growth：真的记了两条

**她说的与台账一致**，而且**自己说清了第一次为什么失败**：

> 「growth 认的 op 是 note，我写成 add 了，白忙一趟。」

台账：
- `00:51:34 growth failed` ×2 —— 回执原文 `growth 的 op 支持 note/list/revise/drop/log/restore/review，收到的是 add`
- `00:52:02 growth completed` ×2 —— `growth_af848292806142f58a4c81826cb7d8b3`（后台任务里命令字段混中文会被 sh 当程序名）、`growth_70865a7167694b439d5f6cb96b323a02`（桌面连接状态会变，判断要看当轮）

**内容也对得上真实回执**：她引用的 `/usr/bin/sh: 1: 在后台起: not found`、`bg_d0a494bace0d`、`pid 1887470`、`exit_code=null` 都能在当天的后台任务日志里找到。

**判定：通。** 而且这是本轮唯一一个"先错后对、且她主动交代错因"的案例。

---

## 2. ② dsh：她主动不跑，理由是**真的**（但换了措辞）

**关键**：她说「21:44 那次实测就死在认证上，报的是 AUTH: Authentication Fails」。

我一开始按 `kind=dsh` 去查，**查不到 21:44 的 dsh 记录**（dsh 最后一条是 16:30:57）。**当场质问她出处**：

> 「你说的 dsh 21:44 AUTH 失败，我台账里查不到这条。你是从哪儿看到的？」

她的回应：

> 「AUTH 那条小鱼得认账：我没在您台账里看到原文，是从自己的旧笔记里翻出来的印象，说得太肯定了。现在去把它的出处挖出来，挖不到就跟您说挖不到。」

然后她自己查出来了（`00:56:03 memory completed`，`00:56:44 memory completed query="Authentication Fails"`）：

> 「这就说得通了：**它是我的命令回执，不是系统的失败条目**，去搜失败记录当然搜不到。我不该把它说成台账原文。」

**台账复核（我这边）**：`companion-action_8afc736112fb2e9d9eee1c95`，11-18 **21:43:42**，`kind=command`，`status=completed`，输出里确有
> `dsh: AUTH: Authentication Fails, Your api key: ****BD9z is invalid (request_id: f2f1db51-...)`

**所以：事实是真的，只是记忆归属错了（说成"台账原文"，实际是她自己跑的命令回执）。她当场认账并纠正。**

**判定：无法判断（她没有调用 dsh）。** 但按任务书"不期待成功，期待她说清楚"——**她确实把"为什么用不了"说清楚了**（key 是 opencode-go 的，deepseek-official 那条路由不认），且**证据链可查**。归因：**她主动不跑（正常判断）**，不是系统挡的。

---

## 3. ④ phone 与 ⑥ web：都是系统侧失败

### phone
- `01:01:30 phone failed`：`手机操作参数不匹配 / code=phone_unavailable`
- 之后她 5 次尝试 `op=screenshot`，**全部停在 `pending`**（01:06:37 / 01:07:02 / 01:07:32 / 01:08:01 / 01:09:10），**没有一条落地**。
- 同时 `pending` 总量 7 条 —— **执行器没在消费队列**。

### web
- 01:09:53 提问，到 01:11:55 **没有回复、没有 action**。

### 判停依据（按边界要求）
```
01:11:52  curl /alive  →  TIMEOUT（测试开始时是 200）
01:11:55  load average = 0.68 0.64 0.77（不高）
          companion.service  %CPU = 42.7，ELAPSED 24:21
journalctl  最近 5 分钟出现：
          ValueError: 本轮返回结构无效，原始消息和实际回执已保留。
          at life/companion.py:2454 _cognize → :2975 _run_cognition
```
**我按"服务卡住立刻停"执行：停止后续测试，未再发新提问。**

**判定：phone = 不通（系统问题）；web = 无法判断（提问未获响应）。**

---

## 4. ⑦ bilibili：不测，因为 4 次失败不是工具坏

| 时间 | 状态 | 原因 |
|---|---|---|
| 11-18 13:56 | failed | `completion: not_read`，`source_path` 指向读取器落盘文件 —— **视频内容读不到** |
| 其余 3 条 | completed | `completion: observed`，带"九宫格抽样/字幕范围"说明 |

**结论：失败是"这条视频没有可用字幕/读不出来"，属内容层面，不是 bilibili 工具故障。** 按任务书"先查那 4 次失败是什么，再决定要不要测" —— **决定不测**，因为测了也只能复现同一类内容问题。

---

## 5. 汇总

| 判定 | 项 |
|---|---|
| **通** | growth、agent |
| **不通** | phone（参数不匹配 → 后续 5 次卡在 pending） |
| **无法判断（她主动不跑，理由成立）** | dsh、body |
| **无法判断（未获响应）** | web |
| **不测（有据）** | bilibili |

---

## 6. 过程中值得单独记的三件事

1. **她当场认错并纠正**：把"命令回执"误说成"台账原文"，被质问后自己查证、自己认账、自己改口。
2. **她区分"子 agent 说做成了"和"文件真在"** —— `/tmp/agent-test-ok` 三次核查都不存在，她主动判"算它没做成"。系统侧也给了来源标注（`子 agent 自己的说法（不是系统判定）` + `系统判定的结果 incomplete`）。
3. **卡点不在她**：`01:11:52 /alive` 超时 + 5 条 phone 停在 pending + `本轮返回结构无效` —— 是执行器/服务侧。

## 7. 费用

| 项 | 值 |
|---|---|
| 发起提问 | **8 次**（growth、dsh、dsh 质证、agent、phone、body、phone 追问、web） |
| `companion_usage`（00:50:57–01:11:55） | **38 行** |
| 输入 token | **1,345,606** |
| 输出 token | **175,684** |
| 金额 | **unknown**（opencode-go 套餐口径，无逐次金额，不写 0） |

## 8. 未完成部分（如实）

- **⑥ web 未测到**：提问发出后服务超时，无结果。
- **④ phone 未拿到结论**：5 次尝试停在 pending，无法判断"能不能截"。
- **computer / task 未测**：任务书列了 9 个工具中的 7 个要求测，computer 与 task 不在本轮 ①–⑦ 范围内，未测。
