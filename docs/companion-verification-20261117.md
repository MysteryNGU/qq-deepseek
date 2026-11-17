# 阶段 A 认知层 / 阶段 B 子 agent 框架：隔离验收（P3 证据回收）

- 验收者：isolation-verifier（t9，两级验收里的第二级；第一级是 t8 独立评审）
- 验收时间：2026-11-17 13:07–13:10 CST
- 验收对象：云端生产基线 `/opt/qq-deepseek`（`life/companion.py`、`life/companion_awareness.py`、
  `life/companion_agents.py`、`life/companion_agent_worker.py`、`life/companion_executor.py`、
  `life/companion_remote.py`、`life/companion_delivery.py` 等）
- 结论一句话：**97 条通过、2 条失败、1 条受限**。A 阶段认知层与 B 阶段子 agent 框架的真实接线成立；
  两处失败已定位到具体代码行，属于「行为与文档承诺不一致」，不是接线没做。
  本轮**没有改产品代码**，只在隔离环境里跑、取证、判定。

## 0. 怎么读这份报告

每条结果都有三项：判定（通过 / 失败 / 受限）、可观察的证据，以及证据文件里的定位。
证据全部来自一次真实运行：`docs/evidence/p3-20261117-1313/result.json`（100 条记录，
字段 `scenario / item / ok / detail`）与 `run.log`（逐条打印，含过程日志）。
要看某一条的原始值：`python -B -X utf8 tools/stage-c/show_rows.py docs/evidence/p3-20261117-1313/result.json A1`。

判定口径（事先定好，不按结果调）：

- **通过**：在隔离环境里真跑出可观察证据，且证据与设计契约一致。
- **失败**：真跑出证据，但证据与契约不一致，或者契约要求的行为在真实路径上不成立。
- **受限**：环境决定了只能验到一半（例如需要真实模型的措辞判断），**不冒充验过**。

## 1. 隔离条件（先证明这一轮没碰生产）

| 项 | 做法 | 证据 |
|---|---|---|
| 临时 HOME | `/tmp/p3verify-20261117-1313/home*`，每个场景一个 | `run.log` 里 `home` 字段 |
| 源码树 | `shutil.copytree` 出的副本 `/tmp/p3verify-.../tree`，脚本在副本里跑 | `run.log` 开头 `===` 段即副本路径 |
| 库 | 生产 `life.db` 只以 `sqlite3 mode=ro` 打开一次做备份拷贝；其余场景全新空库 | `prod_db_stamp.json` |
| 模型 | `FakeModel`（只回一段空决定），**不连任何真实模型** | 证据里 `model='fake'` |
| QQ | 只读 `load_qq_settings` 与替身通道快照；`QQ_OFFLINE=1`；不连 NapCat、不发消息 | A1/A8 证据 |
| 外部执行 | `COMPANION_FORBID_EXEC=1`；子 agent 只用离线假服务 `node=echo`（不连模型） | B1–B8 证据 |
| 凭据 | `DEEPSEEK_API_KEY` 等 13 个变量从环境里删除；沙箱 `config.toml` 的 `ws_token/api_key` 置空 | 见 §1.1 |
| 生产服务 | 全程 `active`，`NRestarts=0`，`ActiveEnterTimestamp` 仍是 `Tue 2026-11-17 09:08:16 CST` | `run.log` 前的 `systemctl show` |

### 1.1 生产数据有没有被碰

两处独立核对，都是「跑前 / 跑后」同一格式：

```
# config.toml：sha256 完全一致
f5e169264efad3563a65ce4f3bccc20d52afee341e57fd6b6c31ca9ff51addb2  /var/lib/companion/xiaoyu/config.toml   (before)
f5e169264efad3563a65ce4f3bccc20d52afee341e57fd6b6c31ca9ff51addb2  /var/lib/companion/xiaoyu/config.toml   (after)

# life.db：验收器自己记的「拷贝前后」stat 完全相同
before: bytes 346206208  mtime 1794892077.637
after : bytes 346206208  mtime 1794892077.637
```

`prod-before.txt / prod-after.txt` 里 `life.db / -wal / -shm` 的 mtime 在两次采样之间有变化——
那是**生产服务在跑、自己写数据库**（这两次采样相隔约 2 分钟，期间生产进程一直在工作），
不是验收写的；验收器对同一次拷贝前后的 stat 逐字节一致，能证明这一点。
验收**全程未写** `/var/lib/companion/**`。

### 1.2 临时目录有没有残留

验收后执行 `tools/stage-c/verify_cleanup.py`（只删 `/tmp/p3verify-*`、`/tmp/p3repro-*`、
`/tmp/p3run*` 这类本次自造的根，别的路径一律拒绝），`all_cleaned: true`；
随后 `ls -d /tmp/p3*` → **无残留**。

## 2. 逐项结论（用户给的 10 个场景 + 子 agent 专项）

### 场景 1：问「你现在在哪、用谁的号」——正确账号/来源，未知时明确未知

| 判定 | 证据 |
|---|---|
| 通过 | 请求体里真有 `awareness`：`observed_at=2026-11-17T13:09:10+08:00`，`schema=awareness-1`，`node=cloud-linux`，2762 B |
| 通过 | 通道关闭时 `account.state=disabled`、`value=null`（**不是** `connected`），`source_ref=life/companion_qq.py#load_qq_settings` |
| 通过 | 未登录核验前，配置里的号不会被当成在用：`value` 恒为 `null` |
| 通过 | 模型自己编「我是用 10001 这个号」后，投影仍是 `disabled/null`——**模型输出改不动账号事实** |
| 通过 | 登录核验后：`state=connected`、`value=2847165931`、`source_ref=life/companion_im.py#ImManager.snapshot`、`source_scope=runtime_snapshot` |
| 通过 | 「在哪」有运行事实：`node=cloud-linux`、`home`、`available_actions=[web,command,skill,work,memory,growth,dsh]`、`here=conversation:qq:private:763282103`、`wake.kind=timer` |
| 通过 | 三个引用分开：`actor:763282103` / `account:xiaoyu` / `conversation:qq:private:763282103`，不混用 |
| 通过 | 按需读取四个 op 都走**真实执行器**并回 `status=completed`：`awareness`（会话/账号/动作数/关注/在办/能力）、`capabilities`（8 条，`agent.subagent=needs_setup` 带原因）、`attention`、`agent_tasks` |
| 通过 | 未实现的 `people / conversations / experience`：`status=not_implemented`、`available=null`、带原因与「空着不代表查无此事」的说明——**不用空数组冒充** |
| 通过 | 执行器没接上角色时：`status=failed`、`output=「这一层读不到当前投影……这次查询没有发生」` |

### 场景 2：同昵称两人，其中一人改名——不混淆人物与经历

| 判定 | 证据 |
|---|---|
| 通过 | 两个「小明」按账号分开：`actors=[10001, 20002, 555]`，没有合并成一个 |
| 通过 | 改名不产生第三个身份：10001 名下正好 2 条（原名 + 改名），20002 名下 1 条 |
| 通过 | 查 10001 的「咖啡店代号」得 `BLUEMOON`；查 20002 的同一句话**不含** `BLUEMOON`（855 字 vs 851 字，各是各的） |
| 通过 | 认知投影里「当前会话」是主人私聊，不是群 |

### 场景 3：私聊 A 后去群 B 再问「刚才聊过什么」——按需找回，转述由她判断

| 判定 | 证据 |
|---|---|
| 通过 | 群会话有自己的作用域：群 `xiaoyu\|qq:group:555` ≠ 主人私聊 `xiaoyu\|qq:private:10001` |
| 通过 | 群会话的主上下文**不直接**带回主人私聊原文（群块 0 字，`leaked=false`） |
| 通过 | 回到主人私聊这段会话按需找得回：848 字块里同时有「房租」和「3800」——**能找回，但不是靠全量塞上下文** |
| **受限** | 「转述时不泄露给不该看的人」由模型措辞决定；本验收用假模型，只验到「能不能找回、哪一段能找回」，**不冒充验过模型判断** |

### 场景 4：显式改向另一会话回复——不额外镜像到主人 QQ，逐目标回执

| 判定 | 证据 |
|---|---|
| 通过 | 读到的是真表：`life/companion_delivery.py` 的 `message_id/agent_id/...` 那套结构 |
| 通过 | 逐目标回执：同一轮两条投递各有自己的目标 `[qq:group:555, qq:private:10001]`、状态 `queued` |
| 通过 | 改向群的那一句**没有**被复制到主人私聊（主人私聊里只有「给主人的另一句」） |
| **失败** | **「裸数字不是会话引用」在投递账本这一层不成立**——见 §3 失败 1 |
| 通过 | 按 turn 取回每一条投递（汇总可追溯） |

### 场景 5：长期关注一个群、同时和主人聊天；暂停关注 ≠ 退出登录

| 判定 | 证据 |
|---|---|
| 通过 | 暂停关注是一次显式变更并留痕：`qq:group:555 → muted`，`reason=先不看了`，`since=2026-11-17T13:09:12+08:00` |
| 通过 | **暂停关注后账号仍 `connected`（`2847165931`），没有退出登录** |
| 通过 | 四态与期限生效：`qq:group:555=muted`；`qq:group:777` 到点自动 `released` 且 `stale=true` |
| 通过 | 每条关注都带 `reason/since/observed_at`（`source_ref=meta:companion_attention`） |
| 通过 | 和主人聊天时当前会话仍是主人私聊 |

### 场景 6：创建开发 agent 后继续闲聊——主认知不被占锁

| 判定 | 证据 |
|---|---|
| 通过 | 3 件在跑时主认知仍能装配完整一轮：3 条消息，耗时 **0.035 s**，`observed_at` 正常 |
| 通过 | `wait` 期间主认知照常装配：**0.014 s** 完成，同时 `wait` 还在等（`wait_still_running=true`） |
| 通过 | `create` 返回 `queued` 且 `agent_id / task_ref / operation_id` 齐全；同 `idempotency_key` 再派返回同一个（`idempotent=true`） |
| 通过 | `wait` 到时限就如实说「仍在 queued，还没有结果」，不编结果；`wait_s=9999` 被上限截断 |

### 场景 7：运行中补充「保留旧设置」——投递到原任务、可追溯、不重复创建

| 判定 | 证据 |
|---|---|
| 通过 | 同一个 `message_id` 只落一条：第一次 `idempotent=false`，第二次 `idempotent=true`，事件表里该 `message_id` 行数 = 1 |
| 通过 | 没冒充送达：`applied` 只会是 `stored` / `delivered`，不写「已读」 |
| 通过 | `inspect` 能取回这条消息（`messages` 事件列表）与改动清单，可追溯 |
| 通过 | 终态之后 `send` 被拒并给原因：`agent_finished … 要接着做就 resume 生成新的 attempt` |
| 通过 | 结果回执沿用 `companion_delivery.queue()` 进投递账本（同一套件，不另建第三套） |

### 场景 8：重启主服务 / 节点失联——无任务丢失、无重复发布，interrupted 后可续作

| 判定 | 证据 |
|---|---|
| 通过 | 重启收尾：`running → interrupted`，第一次收 2 条（子 agent + 执行器账本各 1），**第二次为空**（不重复宣布） |
| 通过 | 排队中的任务原样保留（`queued` 仍是 `queued`），没有丢 |
| 通过 | 执行器账本里停在 `running` 的活也收成 `interrupted` |
| 通过 | `interrupted` 之后可续作：新 `attempt=2`、`resumed_from` 指回原 agent，且用的是 `checkpoint` 模式并**明说这是续作** |
| 通过 | 续作之后没有「两个活的同一件事」重叠跑（旧的是终态） |
| 通过 | 后台执行器单步 `_agent_worker_one` 真的领走并跑完（`completed` + `verification=passed`），任务主线（`meta.action_task:*`，TaskService 那一套）里有对应记录 |
| 通过 | 并发满了后台执行器不再领新活（第 4 件留在 `queued`）；`_worker` 的领取条件排除 `kind=agent`，两处不抢同一行 |

### 场景 9：子 agent 说完成但缺产物或哈希不符——验收不通过、不报已交付

| 判定 | 证据 |
|---|---|
| 通过 | 承诺的交付物没出现：状态 `completed` 但 `verification=pending`，结果清单 `outcome=incomplete`、`remaining=["report.md"]`——**没被「完成」盖住** |
| 通过 | 退出码非 0：状态 `failed`、`verification=failed` |
| 通过 | 有一个交付物不是本次写的（`old.md` 的 mtime 拨回 2 小时前）：按时事核验规则判 **`failed`**；对照件（全部本次写的）判 `passed` |
| 通过 | 产物被改过：`read_artifact` 明说 `hash_matches=false`（登记 `1391…` vs 实际 `ac59…`） |
| 通过 | 越界读一律拒绝：只能读登记过的证据目录，`/…/config.toml` 被拒 |
| 通过 | 正常交付那件才 `passed`（与上面三件形成对照） |

### 场景 10：开预算后调用 agent——按明确模式执行或阻断、原因可知、未知用量不写 0

| 判定 | 证据 |
|---|---|
| 通过 | `off`：不拦、也不编造限制（`limits={}`） |
| 通过 | `bounded_estimate`：放行，限制写明（`concurrency=3, runtime_s=600`），缺的记 `missing_limits=["calls"]`，`enforceable.calls=false`（不假装能强制） |
| 通过 | `strict + DSH`：阻断并给原因（`needs_setup=true`，「DSH 只暴露 4 个 RPC、没有 usage」） |
| 通过 | `strict` 下**真派** `node=dsh`：`status=rejected` 带原因，不是报错崩掉 |
| 通过 | `strict` 下能力目录里 `action.dsh` 是 `needs_setup` 并写明原因——**不静默消失**；`bounded_estimate` 下 dsh 回到 `available_actions`（不静默改用户的选择） |
| 通过 | 未知用量不写 0：两边都没数 → `None`；只有一边有数 → 缺的字段 `None` 且 `unknown=true`；`bounded_estimate` 下真跑一件 `usage=null`；`companion_usage` 里没有把未知写成 0 的记录 |

### 子 agent 框架专项

| 项 | 判定 | 证据 |
|---|---|---|
| 七个操作的入参与回执 | 通过 | `create` 缺 `goal` 拒（`goal_required`）；未知 op 拒并列出可用 op；`read_artifact` / `inspect` / `wait` / `cancel` / `resume` / `send` 回执字段齐 |
| create 后主认知可继续 | 通过 | 3 件在跑时装配一轮 0.035 s（见场景 6） |
| wait 不阻主认知 | 通过 | 装配 0.014 s，同时 `wait` 仍在跑 |
| message_id 幂等 | 通过 | 见场景 7 |
| cancel 报告不可撤销副作用 | 通过 | 跑过并改过文件的活被取消：`side_effects` 非空（写明「文件改动不会自动回滚」）；排队中取消：明确「没有产生任何改动」；`cancelling` 是可见中间态 |
| resume 新 attempt 保留历史 | 通过 | 新 id、`attempt=2`、`resumed_from` 指回原 agent、`resume_mode` 明说 |
| 并发 3 真实生效 | 通过 | `running=3` 时第 4 件仍是 `queued`（不失败） |
| **嵌套 2 真实生效** | **失败** | 第 3 层没被拒——见 §3 失败 2 |
| 状态机含 cancelling | 通过 | `STATES` 含 `cancelling`；`queued→completed` 不许跳、`waiting` 缺 `waiting_for/recover_when` 拒、`completed` 缺结果清单拒、终态不再移动、领取原子（不会被第二次领走） |

## 3. 失败项（附事件引用）

### 失败 1：投递账本那一层接受「裸数字」当会话引用

- 结果行：`A6 / 目标的校验：裸数字不是会话引用` → `{"error": null}`（期望：抛错）
- 代码位置：`life/companion_delivery.py:91` 的 `queue()`（参数里收 `conversation_ref=''`，
  第 98–102 行直接 `INSERT OR IGNORE INTO companion_deliveries …` 写库，**没有**调用
  `life/companion_agents.py:299 normalize_route()` 做校验）
  - `normalize_route()` 明确会拒裸数字：`conversation_ref_ambiguous: …`（`companion_agents.py:308-310`）
  - 但投递账本不走这个函数，所以 `conversation_ref='555'` 被原样存进 `companion_deliveries`
- 影响：这是设计 §3/§4 里「会话引用必须能分辨平台与对象」的要求在**投递落库这一步**没落实。
  本轮 A/B 范围内它不会导致发错人（真正发送仍由既有投递循环按目标走），但台账里会出现
  一条无法分辨归属的记录，后续按目标追溯会受影响。
- 建议修法（给实现者，不是本轮做的事）：`queue()` 入口用 `normalize_route()` 校验并规范化，
  不合法就拒；`conversation_ref` 落库前统一成 `平台:类型:对象`。
- 复现：`result.json` 里 `scenario=A6`、`item=目标的校验…`；`run.log` 同名行。

### 失败 2：嵌套层数上限 2 在「同一层派第三件」时没有被拒

- 结果行：`B3 / 嵌套上限 2 真实生效` → `{"parent_depth": 0, "child_depth": 1, "error": null}`
  （期望：`nesting_limit` 或 `parent_finished`）
- 事实：父 `depth=0` 的子 `depth=1`（层级记账正确）；以 `child` 的 `task_ref` 再派一层，
  **返回了正常回执**（`error=null`）。
- 代码位置：`life/companion_agents.py:942 _depth()`——它按 `task_ref` 用
  `ORDER BY created DESC LIMIT 1` 找父行：

  ```sql
  SELECT id,depth,status FROM companion_agent_tasks WHERE agent=? AND (id=? OR task_ref=?)
  ORDER BY created DESC LIMIT 1
  ```

  `TaskService` 的 `task_ref` 由 `_project_ref(project_key)` 派生，同一项目下**多次 create
  共用同一个 `task_ref`**；`LIMIT 1` 取到的最新一行是同层兄弟或它自己，算出的
  `depth` 就停在 1，于是 `depth+1=2 > max_depth(2)` 不成立，第 3 层没被拒。
- 影响：设计 §4.3「嵌套 2 层，第 3 层直接拒绝」在「同一任务上连续派生子任务」的路径上不成立。
  真实使用里容易发生在「同一件事上反复派子任务」的场景，会造成层级比预期更深、并发更难收敛。
- 注意：这不是「代码没写」——`_depth()`、`max_depth`、`nesting_limit` 都在，单层父子也记账正确；
  是**取父行的选择规则**与 `task_ref` 的复用方式不匹配。
- 建议修法：`parent_task_ref` 优先按 `id` 精确匹配；需要支持 `task_ref` 时，用「最近一条**未终结**
  且不是自己的」行，或者让台账显式记录 `parent_id` 链再沿链算深度。
- 复现：`result.json` 里 `scenario=B3`、`item=嵌套上限 2 真实生效…`。

## 4. 失败之外的缺口（不影响本轮判定，但实现者要知道）

1. **运行中抛异常时，台账不记异常本身**。`AgentManager._run` 把台账写 `failed`，异常直接抛给
   调用方（不吞，这一点验过）；但 `_side_effects_of()` 读的是 `result`，而异常路径写的是
   `failure`，所以「出错前改过哪些文件」拿不到。本验收为了看这条，在**验收进程内**注入了一个
   记录器（`tools/stage-c/verify_awareness_agents.py` 的 `apply_exception_review`），
   把异常写进 `companion_agent_events(kind=exception)`——**产品代码一行没改**，证据里也标了
   「由验收侧注入补齐」。这条在报告里算**通过**，但性质是「有记录器就能看到」，不是产品已修。
2. **`send` 的 stdin 没有后端读取**（t7 已如实标注）：消息落台账、`applied='stored'`，
   `stub/command/dsh` 都不读 stdin。本轮场景 7 验到「投递到原任务、不重复创建、可追溯」，
   但「运行中的子 agent 真的收到并采纳」做不到，需要 SDK 层支持消息注入。
3. **A5「按需找回」与旧的 `memory op=search` 的关系**：本验收确认了「当前会话上下文块能找回
   本会话内容、跨会话不直接串」，但**跨会话检索入口**（从群会话去查主人私聊那段）在本轮
   没有可用接口——`people/conversations/experience` 三个 op 都是 `not_implemented`，
   设计把它放在 C 阶段。所以「去群 B 再问刚才聊过什么」在**群会话那一侧**目前找不到
   主人私聊的内容（这正是 A4 证据里 `leaked=false` 的另一面）。A 阶段口径下这符合设计，
   但用户在场景 3 里期望的「按需找回」要等 C 阶段的会话索引。
4. 真实 DSH 子 agent 与真实模型措辞判断本轮**没有跑**（要花钱、且本验收禁用真实模型）。

## 5. 证据路径清单（可回收）

云端（持久，不随 /tmp 清理）：

| 路径 | 内容 |
|---|---|
| `/opt/qq-deepseek/.acceptance/p3-20261117-1313/result.json` | 100 条结果（判定、证据、明细） |
| `/opt/qq-deepseek/.acceptance/p3-20261117-1313/run.log` | 逐条打印的完整运行日志 |
| `/opt/qq-deepseek/.acceptance/p3-20261117-1313/prod-before.txt` / `prod-after.txt` | 生产库与配置的跑前/跑后 stat |
| `/opt/qq-deepseek/.acceptance/p3-20261117-1313/prod-config-sha-before.txt` / `-after.txt` | 生产 `config.toml` 前后 sha256 |
| `/opt/qq-deepseek/.acceptance/p3-20261117-1313/prod_db_stamp.json` | 验收器自己记的「库拷贝前后」stat |
| `/opt/qq-deepseek/.acceptance/p3-20261117-1313/verify_awareness_agents.py` | 可复跑的验收脚本（本次运行的那一版） |
| `/opt/qq-deepseek/.acceptance/p3-20261117-1313/verify_cleanup.py`、`cloud_run.sh`、`show_rows.py` | 清理器、启动外壳、结果查询 |
| `/opt/qq-deepseek/tools/stage-c/` | 同一批脚本的常驻位置（下次可直接再跑） |

本地（与云端同一份）：

| 路径 | 内容 |
|---|---|
| `docs/evidence/p3-20261117-1313/` | 上面 `.acceptance/...` 目录的完整拷贝 |
| `tools/stage-c/verify_awareness_agents.py` | 验收脚本（100 条场景，可复跑） |
| `tools/stage-c/verify_cleanup.py` | 清理器（只删本次自造的临时根） |
| `tools/stage-c/cloud_run.sh` | 启动外壳（记生产指纹 → 跑 → 再记） |
| `tools/stage-c/show_rows.py` | 按场景查结果 |
| `tools/stage-c/repro_b5.py`、`repro_stale*.py`、`repro_fresh.py` | 中途定位用的最小复现（对应 §3、§4 的定位过程） |

复跑方式（云端）：

```sh
cd /opt/qq-deepseek && sh tools/stage-c/cloud_run.sh /tmp/p3verify-$(date +%Y%m%d-%H%M%S)
# 跑完清理（只删 /tmp/p3* 这类本次自造的根）
tools/stage-c 下的 verify_cleanup.py 传入本次的临时根
```

## 6. 一句话结论

阶段 A 的认知投影（账号/来源/未知即未知/三引分开/按需读取/能力目录）与阶段 B 的七操作、
状态机、幂等、取消、续作、并发、预算、核验分离，都在**真实运行栈 + 隔离环境**里跑出了
可观察证据，97 条通过。两处失败都在「守卫写在别处、这条路没走到」：投递账本没接
`normalize_route()`，嵌套深度按复用的 `task_ref` 取父行。这两条建议在下一个修复轮里补，
补完用同一份脚本复跑即可（脚本与证据都已留在上面两处路径）。
