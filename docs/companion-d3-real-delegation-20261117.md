# D3：经生产入口跑一次低风险只读真实 DSH 委托

- 验收者：isolation-verifier（t17，attempt 3）
- 时间：2026-11-17 17:23:29（正式跑，成功）· 17:21:59（前一次成功但取证不完整）· 16:37:50（修复前，失败）
- 授权口径（captain 16:35 / 17:18 两次批准，未再追问）：费用不设限制、最坏约 0.08 元、用量记 null 不写 0；
  环境 A（临时 HOME + 生产库只读副本）；空临时目录只读；`runtime_s=300s`、仅 1 层、失败最多原样重试 1 次。
- 证据：云端 `/opt/qq-deepseek/.acceptance/20261117-172329-d3/`（成功）、`20261117-172159-d3/`（同样成功，
  取证脚本在汇总处崩溃）、`20261117-163750-d3/`（修复前失败）；本地 `docs/evidence/d3-*` 同一份。

## 结论

**跑通了。** 链路 a→b→c→d→e 全部走通，且**两轮独立实跑结论一致**。
子 agent 真实读到探测文件里的随机 token 并原样回报（`D3-34230CDD0FD7`），全程没有写文件、没有碰生产库。

但有一条需要如实标注：**第一轮（172159）我的取证脚本在汇总处崩溃、第二轮（172329）有一条断言判错**，
两处都是**我脚本的缺陷，不是产品缺陷**（详见 §2.6）。产品侧唯一的"未满分"是
`verification` 停在 `pending`（§2.4），那是核验规则决定的，不是这次跑失败。

---

## ① 模拟测试通过（假模型 / 隔离环境已验证的部分）

这些在跑真活之前就已确认，不依赖本次真实调用：

| 项 | 证据 |
|---|---|
| 隔离就位 | `home=/tmp/p10d3-…/home`、`life_db=data/life.db`（生产库只读副本）、`budget_mode=off`、`dsh_paths=true` |
| 决策入口受理 `kind=agent` | `(a1)` `action_id=companion-action_893ce278…`、`status=pending` |
| 认领进台账 | `(a2)` `_agent_pending_one`：`claimed=true`、台账 `queued`、`origin=dsh` |
| 生产台账未写 | 跑前跑后 `companion_agent_tasks=0`、`companion_agent_events=0`、agent 类动作=0 |
| **零成本 initialize 预检**（本次新增，修复后先跑） | `tools/stage-c/d3_initialize_check.py` 走同一条 `_spawn`：`launch=` node + `lib/bin.js`、注入 `DEEPSEEK_API_KEY`/`DEEPSEEK_BASE_URL`/`DSH_PERMISSION_MODE`、worker 日志 `Initialized. The working directory is ready.`、拿到 `session_id` |

预检这一步是这次新加的：**在花钱之前先证明"能起来"**。它把上次那个 `FileNotFoundError` 这类问题挡在了付费之前。

## ② 真实链路通过（本次 D3 真正跑通的环节，逐条证据）

正式跑：`sh tools/stage-c/d3_run.sh real` → `D3-SUMMARY {"mode":"real","total":13,"passed":12,"failed":1,"seconds":46.7}`

| 验收项 | 判定 | 证据（取自 `20261117-172329-d3/d3.log` 与 `d3-result.json`） |
|---|---|---|
| **a. 委托被接收** | ✅ | `(a1)` 决策入口受理：`action_id=companion-action_893ce278fdde983327b7155d`、`status=pending`、`kind=agent`；`(a2)` `_agent_pending_one`：`claimed=true`、`action_status=queued`、`agent_id=companion-agent_5315db2d5d130f65a9caab06`、`task_status=queued`、`origin=dsh` |
| **b. worker 实际执行** | ✅ | `(b1)` 台账执行器 22.0 s 完成、预算 300 s 未超时；`(b2)` **真实子进程**：`929446 …/python -B /opt/qq-deepseek/life/companion_agent_worker.py …/agent-companion-agent_5315db….request.json`；`(b3)` **模型真实回复**：worker 日志里出现 `placeholder-token: D3-34230CDD0FD7` + 摘要；`(d1)` 台账存了 `session_id=session-d200482d60704299afa932b3e064ff48` |
| **c. 结果返回小鱼** | ✅ | `(c2)` 结果轮被唤醒：`companion_wake / self_observation` 事件 + `companion_activity` 里出现「子 agent 有了结果：completed」 |
| **d. 状态正确落库** | ✅（含一点保留） | `(d1)` 台账 `status=completed`、`verification=pending`、`attempt=1`、`outcome=completed`、事件序列 `created→receipt→running→completed`；`(d2)` 回执摘要含 token：`found_in_result=true`、`found_in_log=true` |
| **e. 预算与费用来源** | ✅ | `(e1)` `companion_usage` 有这次记录 `purpose=executor`、`model=deepseek-flash`、`usage="null"`；**没有任何一条把未知写成 0**。`spending_calls` 未因这次新增（预算模式 off，`reserve()` 直接放行不记账——与 §0 授权口径一致） |
| **f. stored 不得说成已收到** | ✅ 未触达 | 本次没走 `send`；台账状态与回执只说 `completed/pending`，没有说「worker 已收到消息」 |

**只读约束**：`(b4)` 探测目录跑完仍只有 `probe.txt`、`probe_content_changed=false`；子 agent 自述也写了
「未写文件、未改文件、未安装、未访问数据库、未联网、未创建子代理」。

**生产侧核对**（前后各一次）：

```
config.toml sha256 : f5e169264efad3563a65ce4f3bccc20d52afee341e57fd6b6c31ca9ff51addb2（前后一致）
service            : active / MainPID=923810 / NRestarts=0 / ActiveEnterTimestamp=17:16:16（未重启）
生产台账           : companion_agent_tasks=0、companion_agent_events=0、agent 类动作=0（前后一致）
/tmp               : 无残留
```

### 2.6 两条"判红但不是产品问题"的项（如实标注）

1. **第一轮实跑（172159）**：链路其实已经跑通（子 agent 回报了 `D3-601EC944826F`），
   但我的取证脚本在 `(b3)` 判定上太窄（只认 `session_id`，当时为 `null`），随后又在汇总处
   `sqlite3.Row.get()` 崩溃，**没拿到 c/d/e 的完整证据**。修正判据与读取方式后重跑一次（172329）。
2. **第二轮（172329）`(c1)` 判红**：`companion_actions` 该行显示 `status=queued`。
   这是 `_agent_pending_one` 把回执（`queued`）写回动作行、而**后续执行由 `_agent_worker_one` 走台账**，
   台账那一侧的 `completed` 在 `(d1)` 里已证。也就是说 `(c1)` 的断言对象选错了层级，不是回执丢了。
   **我没有为了让这行变绿而改判据**；它在报告里保持红，并在此说明原因。

> 两轮实跑（172159 / 172329）**结论一致**：都真实读到 token、都 `completed`、都没写文件。
> 这比"只成功一次"更可信，但仍不足以说明长期稳定（见 ③）。

## ③ 仍未验证

1. **长期稳定性**：两次成功只证明基本链路可用。**并发（≥3 件同时跑）、排队、DSH 会话续接（`resume`）、
   超时中止、进程被杀后的收尾**，一条都没验。
2. **`verification` 为什么是 `pending` 而不是 `passed`**：本次没有交付文件（`artifacts=[]`），
   核验规则里没有证据就不给 `passed`——这是"没有可核验的东西"还是"核验逻辑有缺口"，需要实现者确认。
   按契约"核验与完成分离"这不算错，但**它意味着"完成"目前拿不到 `passed` 这个结论**，下游要清楚。
3. **费用真实值**：`usage="null"`（SDK 不返回逐次用量），本次真实花费**仍然未知**。
   按预算模式 off，`spending_calls` 不记账，所以这次调用在费用账本上是**不可见的**——
   与 §0 授权口径一致，但也意味着**无法事后对账**。
4. **凭据可见性**：本次能跑是因为 `_dsh_env()` 把 `cfg.api_key` 注入子进程环境。
   子 agent 以 `DSH_PERMISSION_MODE=danger-full-access` 运行（与主 dsh 路径同款），
   **它在自己的会话里能读到这个 key**。本轮 goal 限死只读、结果也未越权，但这是**结构性风险**，
   不是本任务能解决的。
5. **真实模型在子 agent 里的自由行为**：本轮 goal 写得极窄。更宽的委托（多步、多文件）表现如何，未验。
6. **嵌套禁止只靠提示词**：本轮"不允许再派子 agent"写在 goal 里，产品侧没有硬拦。

## 4. 修复前后的对照（供后人快速理解这次为什么能跑通）

| | 修复前（16:37:50） | 修复后（17:23:29） |
|---|---|---|
| 台账 | `queued` → `failed` | `queued` → `completed` |
| 子进程 | 起来了，立刻失败 | 起来了，跑完 22 s |
| DSH | `FileNotFoundError` | 模型真实回复，`session_id=session-d200482d…` |
| 子 agent 是否读到文件 | 否 | **是**（`D3-34230CDD0FD7` 原样回报） |

根因（已在上一版报告定位、本轮复核）：`_spawn` 用 `node` 键同时存「传输后端名」和「node 可执行文件路径」，
后者被覆盖成字符串 `'dsh'`；加上 `_launch_args` 未传、凭据未注入。t19 修了这三处。

## 5. 证据与复跑

| 路径 | 内容 |
|---|---|
| `20261117-172329-d3/` | 成功实跑：`d3.log`、`d3-result.json`、前后服务/台账/config 快照、residue |
| `20261117-172159-d3/` | 同样成功的实跑（取证不完整），作为独立第二例 |
| `20261117-163750-d3/` | 修复前的失败实跑 |
| `tools/stage-c/d3_real_delegation.py` | 正式跑脚本（走生产入口） |
| `tools/stage-c/d3_initialize_check.py` | **零成本预检**：只到 initialize，不提交模型回合 |
| `tools/stage-c/d3_run.sh` | 外壳：记前后指纹 → 跑 → 清工作区 |
| `tools/stage-c/upload_verified.py` | 带 sha256 校验的上传（本机 scp 偶发丢字节） |

复跑：

```sh
sh tools/stage-c/d3_init_run.sh          # 先零成本确认能起来
sh tools/stage-c/d3_run.sh real          # 再一次正式委托（会产生少量费用）
```
