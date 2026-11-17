# 小鱼持续认知与子 agent：本轮交付报告（阶段 A + 阶段 B）

日期：2026-11-17。报告人：env-scout（t12）。性质：**交付报告，不改任何实现代码**。
口径来源：`docs/companion-implementation-plan.md`（P1 契约，§7 五档证据定义）。

---

## 0. 一句话结论

阶段 A 认知层与阶段 B 子 agent 框架的**代码已实现、离线测试通过、并在云端隔离环境跑出 97 条可观察证据**；
但**阶段 B 尚未在线上生效**——代码在 11:26–11:53 落到云端磁盘后，`companion.service` 仍是 09:08 启动的那个进程（截至 13:17 已连续运行约 4 小时 9 分，`NRestarts=0`）。所以本轮的「在线接通」这一档，**阶段 A 成立，阶段 B 不成立**。

阶段 C（QQ 多人交流 / 跨会话回忆对外 / 扫码求助）与阶段 D（发布闭环）**本轮未开工**，不是取消。

---

## 1. 五档证据总表

口径见契约 §7。任何一档都不并入其他档；不适用就写「未进行 / 不适用」，不留空。

| 档 | 阶段 A 认知层 | 阶段 B 子 agent 框架 |
|---|---|---|
| ① **代码已实现** | ✅ `life/companion_awareness.py`（43897 B）+ 4 处接线 | ✅ `life/companion_agents.py`（106746 B）+ `life/companion_agent_worker.py`（7049 B）+ 5 处接线 |
| ② **离线测试通过** | ✅ 25 项单测全过；隔离装配验证 | ✅ 隔离验收 97 条通过（详见 §3）；本地契约测试 25 项中 23 项通过（2 项与本轮无关，已证） |
| ③ **在线接通** | ✅ 09:08 重启后生效（证据见 §4.1） | ❌ **未生效**：代码已落盘但进程未重启（证据见 §4.2） |
| ④ **真机验收** | 未进行（阶段 D） | 未进行（阶段 D） |
| ⑤ **实际发布** | 未进行（阶段 D） | 未进行（阶段 D） |

**云端与本地逐字节一致（本轮独立复核）**：七个模块 MD5 完全相同，见 §5。

---

## 2. 阶段 A 交付明细

### 2.1 代码落点

| 文件 | 改动 |
|---|---|
| `life/companion_awareness.py` | 新增，807 行（43897 B） |
| `life/companion.py:45` | import |
| `life/companion.py:1375` | 唯一挂载点：`state['current_state']` 之后注入 `state['awareness']` |
| `life/companion.py:1478` | 超预算时只压 `awareness` 自己这一段（账号/能力事实不降级） |
| `life/companion.py:1490` | 粘住本轮投影，供按需读取复用同一份事实 |
| `life/companion_executor.py:70` / `:105` | `attach_core`；`memory` 分派新增扩展 op 分支 |
| `life/companion_remote.py:158` | 服务模式把核心执行器接到角色上 |
| `life/companion_context.py:27` | `memory` guide 补读投影口径 |

### 2.2 契约落实（对照 P1 契约 §3）

- 投影结构对齐 §3.2：`self.agent` / `self.account` / `runtime` / `here` / `attention` / `recent_experience` / `capabilities` / `agent_tasks` / `wake` / `projection_errors`，每条带 `observed_at` + `source_ref`。
- 三个引用分开：`actor:763282103` / `account:xiaoyu` / `conversation:qq:private:763282103`（验收证据 `result.json` A1 行）。
- 账号事实三源（`ImManager.snapshot` → `CompanionQQ.snapshot` → `load_qq_settings`）；登录核验前 `value` 恒为 `null`，通道关闭即 `disabled`，模型输出改不动（A1/A2 行已验）。
- 注意力四态带 `reason/since/review_at/expires_at/cursor`，过期自动 `released`（A8 行）。
- 能力目录：`agent.subagent` 在 A 阶段恒 `needs_setup`；`dsh` 在 `strict` 下显式 `needs_setup` + 原因，**不静默消失**（这是 P0 定位缺陷的修法）。
- 按需读取复用 `memory` 的 `op`；`people` / `conversations` / `experience` 返回 `not_implemented` + 原因，**不是空数组**（A1 行三条已验）。

---

## 3. 阶段 B 交付明细

### 3.1 代码落点

| 文件 | 改动 |
|---|---|
| `life/companion_agents.py` | 新增（106746 B）：状态机唯一真源、七操作、并发 3 / 嵌套 2、补丁与锁。守卫全在写库前 |
| `life/companion_agent_worker.py` | 新增（7049 B）：`command` / `stub`（离线假服务）/ `dsh`（复用 SDK `resume_session`）三后端 |
| `life/companion.py` | 5 处接线：`DECISION` 加 `'agent'`；构造 `AgentManager`（:446）；`_workers()` + `_agent_worker_one()`；`_worker` 领取排除 `kind=agent`；`start()` 重启收尾；`_settle` 按预算模式记账 |
| `life/companion_awareness.py` | `agent.subagent` 改为证据驱动（台账有 `completed`+`passed` 才 `ready`） |

### 3.2 解除预算阻断（P0 核心问题）

| 预算模式 | 行为 | 证据 |
|---|---|---|
| `off` | `dsh` 保留在 `available_actions`，照常执行 | `stage-b-cloud-check.json` `budget.off.dsh_present=true` |
| `bounded_estimate` | 同上；拿不到用量记未知 | `budget.bounded_estimate.dsh_present=true` |
| `strict` | 移除 `dsh`，且能力目录显式 `action.dsh needs_setup` + 原因 | `budget.strict.dsh_present=false` + `dsh_capability` 含完整原因串 |

**未知用量不写 0**：`merged: null`（两边都无）或 `{..., "cost": null, "unknown": true}`（一边有数）已验。

---

## 4. 在线接通：A 成立，B 不成立（本轮最重要的发现）

### 4.1 阶段 A 确实在线上

```
$ ssh ... systemctl show companion.service -p ActiveEnterTimestamp -p NRestarts
ActiveEnterTimestamp=Tue 2026-11-17 09:08:16 CST
NRestarts=0
```
阶段 A 部署记录 `.acceptance/20261117-090800/after.sha256` 含 5 个文件，`ActiveEnterTimestamp` 正是部署后的 09:08:16。**阶段 A 在线成立。**

### 4.2 阶段 B 已落盘但未生效

实测（2026-11-17 13:17 CST）：

```
$ systemctl show companion.service -p ActiveEnterTimestamp -p NRestarts
0
Tue 2026-11-17 09:08:16 CST

$ stat -c '%y  %n' life/companion.py life/companion_agents.py life/companion_agent_worker.py life/companion_awareness.py
2026-11-17 11:27:26  life/companion.py
2026-11-17 11:37:48  life/companion_agents.py
2026-11-17 11:26:07  life/companion_agent_worker.py
2026-11-17 11:53:49  life/companion_awareness.py
```

改动时间（11:26–11:53）全部**晚于**进程启动时间（09:08:16），进程此后未重启（`NRestarts=0`）。

**交叉印证**：当前 `life/companion.py` 的 sha256 为 `c660430c73d72e78d0d8c925ef3229acdfec71f8fc5c68d3083c8962f5ea9fbb`，而阶段 A 部署记录 `after.sha256` 里记的是 `fd27dd2adb859ed920c74e93b6b467e72e43adb347f4b316d2481bef5207e16a`。文件已变，但改变它的那次写入**没有伴随重启**。

**pyc 佐证**：`life/__pycache__/companion.cpython-312.pyc`（12:07，属主 `root`）与 `companion_agents.cpython-312.pyc` 的源 mtime/size 与当前源码完全匹配（`MATCH=True`），说明 12:07 有一次 root 侧的编译导入，但那不是生产进程（生产进程 `MainPID=699927` 属 `companion`，`/proc/699927/cwd` 自 09:09 未变）。

**结论**：线上跑的仍是阶段 A 那版 `companion.py`，其中**没有** `AgentManager`、**没有** `DECISION` 的 `'agent'` 枚举。阶段 B 的一切行为目前只在隔离环境里成立。

**要让阶段 B 上线，需要一次 `companion.service` 重启，这属于用户决定项**（契约红线：发布/重启需用户确认）。本轮未执行。

---

## 5. 云端与本地双基线：逐字节一致（独立复核）

本轮亲自对两端取 MD5 与字节数，结果完全相同：

| 文件 | 字节 | MD5 |
|---|---|---|
| `life/companion.py` | 180250 | `9aae0eecf8180ee1cd95d7435b38fdc6` |
| `life/companion_awareness.py` | 43897 | `1b300420f174f14b97f3af547b731617` |
| `life/companion_agents.py` | 106746 | `1a720840f22536a925a739bee3a030fd` |
| `life/companion_agent_worker.py` | 7049 | `91fddcd28cae656747f10639cd2cc133` |
| `life/companion_executor.py` | 20834 | `f29aed51d5b57b85dc86ffde91b3313b` |
| `life/companion_remote.py` | 22260 | `9e4dafa694f397e2497197102b7a8cc0` |
| `life/companion_context.py` | 18742 | `353376709b34c65f41c49ad6ba16c5ca` |

但**双基线的残余分化仍在**：

1. **`life/companion_delivery.py` 本地缺失**（云端 10897 B，MD5 `1a8bf3f23938b5e77931a33998d55217`）。这是 t7 已说明的「本地精简检出」。后果：本地跑 `tests.test_agent_contract` 时 `test_delivery_ledger_keeps_targets_apart` 必然失败——**不是回归**。
2. 本地 `tools/stage-a/` **不存在**（云端有，含 `deploy.sh`/`evidence.sh`/`test_companion_awareness.py` 等 9 个文件）；本地只有 `tools/stage-b/` 与 `tools/stage-c/`。
3. 云端 `tools/stage-b/` 与 `docs/stage-b-cloud-check.json` **不存在**；这两样只在本地。阶段 B 的验收证据（`stage-b-cloud-check.json`）是本地留存，云端没有对应副本——与阶段 A（云 `.acceptance/20261117-090800/`）、阶段 C 验收（云 `.acceptance/p3-20261117-1313/`）的留证方式不一致。
4. 云端仍有 47 个 `companion_*.py`，本地 `life/` 不含其中大部分（契约 §2.2 已冻结「只在云端开发、本地不镜像」）。

---

## 6. 隔离验收结果（97 通过 / 2 失败 / 1 受限）

来源：`docs/companion-verification-20261117.md` + `docs/evidence/p3-20261117-1313/result.json`。
**本轮做了独立复核**：直接解析 `result.json`，得 `total=100 passed=97 failed=2 limited=1 seconds=142.8`，与报告一致。

隔离条件（`run.log` 与 `prod-*.txt` 佐证）：`shutil.copytree` 源码副本、每场景独立临时 HOME、生产 `life.db` 只以 `mode=ro` 备份一次、`FakeModel`（不连真实模型）、`QQ_OFFLINE=1`、`COMPANION_FORBID_EXEC=1`、子 agent 只用离线 `node=echo`。
生产侧：`config.toml` sha256 前后一致 `f5e169…`；验收器自记的库拷贝前后 `stat` 完全相同 `(346206208, 1794892077.637)`。

### 6.1 两条失败（已定位到行）

| # | 失败 | 定位 | 性质 |
|---|---|---|---|
| 1 | 投递账本接受「裸数字」当会话引用（`conversation_ref='555'` 被原样落库） | `life/companion_delivery.py:91` `queue()` 第 98–102 行直接 INSERT，**未调用** `life/companion_agents.py:299 normalize_route()` | 守卫写在别处、这条路没走到 |
| 2 | 嵌套上限 2 在「同一层派第三件」时未生效 | `life/companion_agents.py:942 _depth()` 用 `WHERE (id=? OR task_ref=?) ORDER BY created DESC LIMIT 1`；`TaskService` 的 `task_ref` 同项目复用，`LIMIT 1` 取到同层兄弟/自己 | 取父行规则与 `task_ref` 复用方式不匹配 |

### 6.2 一条受限

场景 3「转述措辞由她判断」需要真实模型判断；隔离用 `FakeModel`，只验到「能不能找回、哪一段能找回」。**不冒充验过。**

### 6.3 已知缺口（不影响判定，但要知道）

1. 运行中抛异常时产品不把异常本身写台账（`_side_effects_of` 读 `result`，异常写 `failure`）；验收侧注入记录器补齐并已标注，产品未改。
2. `send` 的 stdin 无后端读取；消息标 `applied='stored'` 不冒充送达。
3. 跨会话检索入口（群 → 主人私聊）属阶段 C；本轮 `people`/`conversations`/`experience` 均 `not_implemented`。
4. 真实 DSH 子 agent 与真实模型措辞判断本轮未跑（要花钱 / 隔离禁用）。

---

## 7. 未完成、受限与需要用户决定

### 7.1 需要用户决定（阻塞「在线接通」）

| # | 事项 | 说明 |
|---|---|---|
| **D1** | **是否重启 `companion.service` 让阶段 B 上线** | 重启即让 `AgentManager`、`DECISION.'agent'`、预算阻断解除在线上生效。契约红线要求用户确认。若不重启，阶段 B 第五档应记为「未生效」，且线上小鱼仍**不能**创建子 agent。 |
| D2 | 两条失败是否本轮修 | 均已有定位与建议修法，属小改动（`queue()` 入口接 `normalize_route()`；`_depth()` 改按 `parent_id` 链）。不修则契约 §4.3「嵌套 2」在特定路径上不成立。 |
| D3 | 真实 DSH 子 agent 是否花一次真实调用验证 | 目前 `node=dsh` 只做了编译与代码审查；要「真实跑通一条委托」得花模型费用。 |

### 7.2 本轮未开工（阶段 C / D，非取消）

- **QQ 多人交流**：群聊接入、他人私聊、逐目标回执、取消自动镜像——**未开工**。现状仍是 `life/companion_qq.py:129` 只接主人私聊。
- **跨会话回忆对外**：**未开工**。
- **扫码求助**：**未开工**。NapCat 侧 `/api/QQLogin/*` 接口在 P0 已确认存在，但云端 `napcat.service` 仍 `inactive`+`disabled`、`qq.json enabled=false`。
- **发布闭环**：**未开工**。

### 7.3 受限（环境决定，非实现缺陷）

- **Android 真机验收**：无。云端能构建（`tools/cloud-build-publish.sh`，证书 `fb83594c…`），但装到手机只能由用户完成。
- **无干净 Windows 环境**：无法验证「没有 Python / 源码目录的干净机器上安装启动」。设计 §8.2 明确「只生成启动器 EXE 不验收为完成」。

### 7.4 双基线残余分化

见 §5 第 1–4 条。最需要注意的是 **`life/companion_delivery.py` 本地缺失**，它会让本地测试出现一个恒定的假失败，容易被误判成回归。

---

## 8. 证据路径清单

### 8.1 云端（持久）

| 路径 | 内容 |
|---|---|
| `/opt/qq-deepseek/.acceptance/20261117-090800/` | 阶段 A 部署证据：`before/after.sha256`、`life-before.tar.gz`（68328 B）、`unittest.txt`、`messages-check.txt`、`online-evidence.txt`、`restart.txt` |
| `/opt/qq-deepseek/.acceptance/p3-20261117-1313/` | 隔离验收：`result.json`（36647 B，100 条）、`run.log`（23692 B）、`prod-*.txt`、`prod_db_stamp.json`、`verify_awareness_agents.py` |
| `/opt/qq-deepseek/tools/stage-a/` | 阶段 A 可复跑脚本（9 个文件，含 `deploy.sh`、`evidence.sh`、`precheck.py`） |
| `/opt/qq-deepseek/tools/stage-c/` | 验收脚本常驻位置 |

### 8.2 本地

| 路径 | 内容 |
|---|---|
| `docs/companion-implementation-plan.md` | P1 契约 |
| `docs/companion-prerequisites.md` | P1 前置核验（31157 B） |
| `docs/cloud-sync-policy.md` | 云端同步规矩 |
| `docs/companion-awareness-baseline.md` | P0 侦察基线（38520 B） |
| `docs/companion-test-isolation.md` | 隔离机制（16676 B） |
| `docs/companion-agent-contract-tests.md` | 契约测试（14423 B） |
| `docs/companion-agents-stage-b.md` | 阶段 B 说明（9406 B） |
| `docs/stage-b-cloud-check.json` | 阶段 B 隔离验收证据（3363 B，**仅本地**） |
| `docs/companion-verification-20261117.md` | P3 隔离验收报告（21601 B） |
| `docs/evidence/p3-20261117-1313/` | 上表的本地完整拷贝（12 个文件） |
| `tools/stage-b/`、`tools/stage-c/` | 阶段 B/C 脚本（**stage-a 本地无**） |
| `tests/companion_test_isolation.py`（24632 B）、`tests/run_isolated_suite.py`（8663 B）、`tests/test_agent_contract.py`（29586 B） | 隔离与契约测试 |

### 8.3 复跑方式

```sh
# 阶段 A 证据
cd /opt/qq-deepseek && sh tools/stage-a/evidence.sh
# 隔离验收（云端）
cd /opt/qq-deepseek && sh tools/stage-c/cloud_run.sh /tmp/p3verify-$(date +%Y%m%d-%H%M%S)
```

---

## 9. 本轮对契约 §6 完成条件的核对

| 条件 | 判定 | 依据 |
|---|---|---|
| A1 投影真进请求体 | ✅ | `result.json` A1：`observed_at=2026-11-17T13:09:10+08:00`、2762 B、`schema=awareness-1` |
| A2 正确引用「在哪/用谁的号」 | ✅（未到模型措辞层） | 通道关闭 `disabled`/`null`；登录后 `connected`/`2847165931`；模型改不动事实 |
| A3 能力不谎报 | ✅ | `agent.subagent` `needs_setup`；`dsh` 在 `strict` 下带原因 |
| A4 按需读取可用且不发言 | ✅ | 4 个 op 均 `completed` + 信封完整 |
| A5 未实现 kind 显式说明 | ✅ | `people`/`conversations`/`experience` → `not_implemented` + `reason` |
| A6 投影失败不打断认知 | ✅ | 注入段异常后整轮仍装配，`projection_errors` 有记录 |
| A7 未知不被补造 | ✅ | 关闭 QQ 后 `disabled`；不写编造 `observed_at` |
| B1 七操作可达 | ✅ | 七个 op 各有真实回执 |
| B2 create 后不占锁 | ✅ | 3 件在跑仍 0.035 s 装配一轮；wait 期间 0.014 s |
| B3 send 幂等 | ✅ | 同 `message_id` 只落一条 |
| B4 运行中补充可追溯 | ✅ | 不产生第二个 `agent_id`；终态 send 拒并指向 resume |
| B5 重启不丢不重复 | ✅ | `running→interrupted` 只一次，第二次空；`queued` 不丢；续作 `attempt=2` 无重叠 |
| B6 产物验收与完成分离 | ✅ | 缺产物 `verification=pending`；旧文件 `failed`；`hash_matches=false` |
| B7 取消终结进程树 | ✅ | `cancelling` 可见中间态；`side_effects` 列出不可撤销项 |
| B8 续接真实生效 | ⚠️ 部分 | `mode=checkpoint`（续作）已验并明示「续作不是恢复」；**`session_id` 复用路径未在真实 DSH 上跑** |
| B9 预算三模式 | ✅ | 三模式行为明确；未知不写 0 |
| B10 并发与嵌套上限 | ⚠️ 部分 | 并发 3 生效（第 4 件 `queued`）；**嵌套 2 失败**（§6.1 失败 2） |
| B11 结果摘要不撑爆 | ✅ | `summary_chars=219` ≤ 1200；`changes=1` ≤ 5 |

**未达成的**：B8 部分（真实 DSH 未跑）、B10 部分（嵌套上限有洞）。其余均达成。

---

## 10. 给用户的一段话

阶段 A（她能知道自己在哪、用谁的号、手上有什么、什么能力真能用）**已经在线上生效**，并且跑出 97 条可观察证据。
阶段 B（她能把一件多步工作交出去、异步拿结果、取消、续作、验收）**代码已在云端磁盘、但还没在线上跑起来**——需要重启一次服务才生效，这一步要您确认。

另外两件需要您定：两条已定位的守卫漏洞要不要本轮就补；真实 DSH 子 agent 要不要花一次模型费用真跑一遍验证。
