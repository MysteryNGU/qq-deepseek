# P0 离线测试骨架：状态机、幂等、路由、身份、续接、预算与失败边界

日期：2026-11-17。任务 t3。**只写测试，未改任何产品代码。**
目标实现：`life/companion_agents.py`（阶段 B，task t7）与既有 `life/companion.py` / `life/spending.py` / `life/people.py` / `life/runtime.py` 的接线点。

## 0. 结论先行

| 项目 | 结果 |
| --- | --- |
| 新增测试文件 | `tests/test_agent_contract.py`（25 个用例） |
| 当前可执行（既有基线） | 9 个，全部通过 |
| 等待实现（契约） | 16 个，以带 `CONTRACT <章节> via <接线点>` 说明的 skip 呈现，**不是通过** |
| 隔离沙箱内效果 | 841 项 **OK**（`skipped=16`），`FAILURE_COUNT=0`，`TREE_CLEAN=true`，`cleaned ok=true`，`home_deleted=true` |
| 全量回归（原地） | `unittest discover` 841 项 OK（816 + 新增 25），257.99 s |
| 外部依赖 | 无网络、无 QQ、无真实模型；每个用例用 `tempfile.TemporaryDirectory()` 新建库 |

## 1. 为什么用「诚实契约」而不是直接写成失败

阶段 B 的实现模块 `life/companion_agents.py` 此刻还不存在（t7 未开工）。如果现在就让 16 个契约用例失败，全量套件会长期红着，等于把“未实现”和“真回归”混成一个数字；如果删掉它们，等 t7 落地时又没人拦住语义漂移。

所以这 16 个用例写成「实现存在就真断言，不存在就带契约说明跳过」：

```python
def pending(self, note):
    if self.module is None:
        self.skipTest(f"{IMPLEMENTATION} not implemented yet — pending: {note}")
```

- `CONTRACT` 字符串写明「计划第几节 / 必须接到哪个既有接缝」，例如
  `CONTRACT 4.2 via create/enqueue must reject waiting without guards`。
- 模块已存在但**接口不对**时不会被跳过，而是**硬失败**并说明缺什么（`contract unmet: …`）。
- 所以「绿」只可能来自真实断言通过；`skip` 永远不算证据，任何汇报里必须单列。

## 2. 覆盖矩阵（任务要求 → 用例）

| 任务要求 | 用例 | 类型 |
| --- | --- | --- |
| 状态机 `queued→running` 与四种终态 | `StateMachineContract.test_legal_transitions_and_illegal_ones` | 契约 |
| `cancelling` 中间态 | `StateMachineContract.test_cancelling_is_intermediate_before_cancelled` | 契约 |
| 终态不可自转、不可跳步 | `ExistingBaselineTests.test_state_machine_rejects_self_loops_and_terminal_moves` | 本地契约自检 |
| `waiting` 必须有等待对象与恢复条件 | `StateMachineContract.test_waiting_requires_waiting_object_and_recover_condition` | 契约 |
| `completed` 必须带结果清单 | `StateMachineContract.test_completed_requires_result_manifest_and_verification_is_separate` | 契约 |
| `verification=pending\|passed\|failed\|unverified` 与完成分离 | 同上（改 `verification` 不得改写 `status`） | 契约 |
| `message_id` 幂等 | `IdempotencyContract.test_send_replays_same_message_id_once` | 契约 |
| 创建幂等（`idempotency_key`） | `IdempotencyContract.test_create_is_idempotent_on_idempotency_key` | 契约 |
| 平台消息天然键幂等 | `IdempotencyContract.test_identity_write_is_idempotent_on_call_key` | 既有实现 |
| `conversation_ref` 路由不串平台 | `RoutingContract.test_route_never_crosses_platform_conversation` | 契约 |
| 私聊/群聊作用域隔离 | `RoutingContract.test_conversation_scope_never_mixes_platforms` | 既有实现 |
| 投递台账按目标区分 | `RoutingContract.test_delivery_ledger_keeps_targets_apart` | 契约 |
| 同昵称不同号不合并 | `IdentityContract.test_same_name_different_account_is_not_merged` | 契约 |
| 改名不产生新人 | `IdentityContract.test_rename_keeps_the_same_person` | 契约 |
| 身份按账号×会话 | `IdentityContract.test_identity_is_per_account_per_conversation` | 既有实现 |
| 预算 `strict`/`bounded_estimate`/`off` | `BudgetContract.test_three_budget_modes` | 契约 |
| 未知用量不写 0 | `BudgetContract.test_unknown_usage_is_never_zero`、`test_reported_cost_is_none_when_the_bill_is_incomplete` | 契约 + 既有 |
| 预算默认不改变用户选择 | `BudgetContract.test_policy_parse_keeps_the_users_choice`、`test_snapshot_reports_unknown_calls_not_zero` | 既有实现 |
| 重启后 `interrupted` 与终态只处理一次 | `RestartContract.test_running_becomes_interrupted_once_and_terminal_states_are_untouched`、`ExistingBaselineTests.test_interrupted_actions_are_kept_not_replayed` | 契约 + 既有 |
| 并发 3 / 嵌套 2，排队不失败 | `ConcurrencyContract.test_queue_and_nesting_limits`、`test_third_nesting_level_is_rejected` | 契约 |
| 不引外部依赖 | `NoExternalSideEffects.test_contract_module_does_not_import_a_model_or_transport`、`test_tests_run_against_a_disposable_database` | 守卫 |

## 3. 运行方式与当前输出

```powershell
# 隔离沙箱（推荐，见 docs/companion-test-isolation.md）
python -B -X utf8 tests\run_isolated_suite.py --only unittest --timeout 900
# 只跑本模块（原地，快）
$env:PYTHONPATH='.test-deps'; $env:PYTHONUTF8='1'; $env:QQ_OFFLINE='1'
python -B -X utf8 -m unittest tests.test_agent_contract -v
```

本模块实测：

```
Ran 25 tests in 1.148s
OK (skipped=16)
```

16 条 skip 的原文示例：

```
skipped 'life.companion_agents not implemented yet —
         pending: CONTRACT 4.2 via create/enqueue must reject waiting without guards'
skipped '… pending: CONTRACT §5 刚性约束 via usage=None / usage.unknown instead of 0'
skipped '… pending: CONTRACT §4.3/§6.2 B10 via 3 running agents, 2 nesting levels, queueing never fails'
```

全量原地回归（含新增）：

```
Ran 841 tests in 257.986s
OK (skipped=16)
```

## 4. 给 t7（阶段 B 实现）的接口清单

`life/companion_agents.py` 只需暴露下面这些符号，16 个契约用例就会自动由 skip 转为真断言。字段名与语义**必须**与 `docs/companion-implementation-plan.md` §4 一致，不得自行发明（§11 的纪律）：

| 符号 | 要求 | 用例 |
| --- | --- | --- |
| `AgentManager(life, home=…)` | 管理器；也接受 `Agents` 这个别名 | 全部 |
| `.create(**args)` | 收 §4.1 的 `create` 入参；返回含 `agent_id`/`task_ref`/`operation_id`/`status=queued` | 创建/并发/幂等 |
| `.transition(agent_id=…, status=…, …)` 或 `advance(state, target)` | 状态迁移；`waiting` 缺 `waiting_for`/`recover_when` 必须拒绝；`completed` 缺结果清单必须拒绝 | 状态机 |
| `.send(agent_id=…, message_id=…, text=…)` | 同 `message_id` 只落一条入站记录 | 幂等 |
| `.inspect(agent_id=…, cursor=…)` | 返回 `status`/`verification`/`messages`/检查点/产物索引 | 状态机/幂等 |
| `.cancel(agent_id=…, include_children=…)` | 走 `cancelling→cancelled`，回执含不可撤销副作用 | cancelling |
| `reconcile_on_start(life, agent_id=…)` | 启动时 `running→interrupted` 只处理一次；终态不动 | 重启 |
| `decide(mode=…, task=…)`（或 `admit`/`budget_decision`） | 三模式判定；`strict`+DSH 必须 `allowed=False` 且带 `reason`、`needs_setup`；`bounded_estimate` 缺上限时 `missing_limits` 可见 | 预算 |
| `merge_usage(known, unknown)` | 未知不得成 0；全未知返回 `None` | 用量 |
| `normalize_route(conversation_ref=…, account=…)` | 解析出 `kind`；裸数字必须拒绝 | 路由 |
| `delivery_ledger` 或 `delivery` | 引用既有投递台账模块，不新造队列 | 路由 |

## 5. 隔离与安全

- 每个用例 `setUp` 建 `TemporaryDirectory`，`LifeRuntime(<临时>/life.db)`；临时 HOME / 临时库由 `tests/run_isolated_suite.py` 再包一层。
- 不导入 `aiohttp`/`websockets`/`openai`/`napcat`/`edge_tts`，并有守卫用例检查 `life.companion_agents` 源码里不出现这些导入。
- 不发 QQ 消息、不连真实模型、不写生产 `life.db`。
- 失败不跳过：只有「模块不存在」才 skip，其余一律硬失败。

## 6. 云端（47.104.8.84）测试树同步清单（待 captain 批准，本任务**未执行**）

云端 `root@47.104.8.84`（已用 captain 提供的 `id_ed25519_194` 验证可登录）：

- `/opt/qq-deepseek` **没有 `tests/` 目录**；`vendor` 只有 312 K（本地 2.1 GB 是本地特有）；`.venv-companion` 有 `jsonschema`/`aiohttp`/`edge_tts`，**没有 `websockets`/`numpy`**；`/tmp` 余量 31 G；`companion.service` active。
- 所以云端不会遇到 t2 里那个 `vendor/.../qq-life-contracts.json` 缺失问题，但需要先有测试树。

建议由 captain 批准后一次性执行（只读源、只写 `/opt/qq-deepseek/tests`）：

```bash
# 1) 最小测试树（约 1 MB）：只放需要跑的模块
scp -i ~/.ssh/id_ed25519_194 -r \
  tests/__init__.py tests/role_fixture.py tests/fixtures \
  tests/test_agent_contract.py tests/companion_test_isolation.py tests/run_isolated_suite.py \
  root@47.104.8.84:/opt/qq-deepseek/tests/
# 2) 全量套件（可选，约 2.4 MB）
scp -i ~/.ssh/id_ed25519_194 tests/test_*.py root@47.104.8.84:/opt/qq-deepseek/tests/
# 3) 云端跑（隔离副本 + 生产库只读副本 + 临时 HOME）
ssh root@47.104.8.84 '
  cd /opt/qq-deepseek && \
  .venv-companion/bin/python -B -X utf8 tests/run_isolated_suite.py \
    --db copy --db-source /var/lib/companion/xiaoyu/life.db \
    --no-deps --timeout 900 --log-dir /tmp/isolated-logs'
```

注意：
- `--no-deps` 是必须的（云端没有 `.test-deps`）；若某模块需要 `websockets`/`numpy`，会在云端如实 `ImportError`，不得用 `pip install -y` 私自补环境。
- 云端若缺 `tests/fixtures/`、`life/` 本地独有模块（如 `role_agent.py` 相关用例），失败要按模块归类记录，不能算进“隔离假失败”。
- 任何部署（含只新增 `tests/`）都需 captain 批准；本任务只写清单，**没有向云端写入任何文件**。

## 7. 与 t2 结论的衔接

- t2 报的隔离环境失败（settings×3、context_efficiency×1）与本模块无关。本模块在隔离沙箱里要么通过、要么带契约说明 skip，不会新增假失败。
- 本模块新增 25 项已计入全量：816 → 841。隔离沙箱内本轮结果：

```
STEP unittest exit=1 seconds=230.9
  | Ran 841 tests in 226.923s
  | FAILED (failures=3, skipped=16)
TREE_CLEAN=true
FAILURE_COUNT=3
  ! FAIL: test_authenticated_save_and_reload_preserve_secret (tests.test_settings.SettingsHttpTests…)
  ! FAIL: test_valid_save_roundtrips_and_keeps_masked_secrets (tests.test_settings.SettingsHttpTests…)
  ! FAIL: test_valid_save_roundtrips_and_keeps_masked_secrets (tests.test_settings.SettingsTests…)
cleaned=… ok=true
home_deleted=true
```

三个失败仍是 t2 那三条 settings，本模块没有新增失败。

## 8. settings×3 的定性（已按 captain 决定改为 del，等待重跑结果）

实测证据：

```
$ python -B -X utf8 -m unittest tests.test_settings -v        # 干净环境、不设 DEEPSEEK_API_KEY
Ran 30 tests in 13.877s
OK

$ DEEPSEEK_API_KEY='' python -B -X utf8 <probe>
load_config api_key = ''            # ← 期望 'private-test-key'
```

根因：`DENY_ENV` 把 `DEEPSEEK_API_KEY` 写成空字符串，`os.environ.get(name, '')` 分不清"没设"和"设成空"，而 `load_config` 把"变量存在"当生效。

按 captain 决定（只改 `DENY_ENV` 一处，防护类不动）：

| 类别 | 变量 | 处理 |
| --- | --- | --- |
| 密钥/凭据 | `DEEPSEEK_API_KEY`、`COMPANION_API_KEY`、`OPENAI_API_KEY`、`ANTHROPIC_API_KEY`、`AWS_ACCESS_KEY_ID`、`AWS_SECRET_ACCESS_KEY`、`HF_TOKEN`、`QQ_WS_URL`、`QQ_WORKER_TOKEN`、`QQ_WORKER_HEALTH`、`ADB_PATH`、`ANDROID_SERIAL`、`OPENAI_BASE_URL` | **del**（`SECRET_ENV`，`env()` 里 `pop`） |
| 防护开关 | `QQ_OFFLINE=1`、`HF_HUB_OFFLINE=1`、`HF_HUB_DISABLE_TELEMETRY=1`、`TRANSFORMERS_OFFLINE=1`、`CUDA_VISIBLE_DEVICES=""` | **保留**（`DENY_ENV`） |

自检断言同步改为两条：`secret_env_absent`（密钥键确实不存在）与 `switch_env_present`（防护开关值仍为 1 / 空串语义），原 `deny_env_applied` 已删除，自检不会因改 del 而挂。

判定标准（未变）：重跑后转绿 → settings×3 = **隔离假失败**，结案；仍红 → **真回归**，按缺陷修，不许绕。

**重跑结果（2026-11-17 11:44，隔离沙箱）：转绿，结案为隔离假失败。**

```
self_check home_is_sandbox=pass / life_db_sha_matches=pass / network_guard_blocks=pass
self_check external_exec_blocked=pass / secret_env_absent=pass / switch_env_present=pass
STEP unittest exit=0 seconds=310.3
  | Ran 841 tests in 304.564s
  | OK (skipped=16)
TREE_CLEAN=true
FAILURE_COUNT=0
cleaned=…companion-test-20261117-105708-38894d ok=true
home_deleted=true
```

即：settings×3 确认为隔离机制（空串被当成"变量存在"）造成的假失败，不是产品回归；`context_efficiency×1` 也随之不再出现——本机沙箱现在会只复制 `VENDOR_INCLUDE` 指定的 vendor 夹具文件，该用例因此拿到输入并通过。

`context_efficiency×1` 的历史定性：**本地沙箱假失败，云端不适用**。本地 `vendor/dsh-bridge-work` 有 2.1 GB 且含该夹具路径；云端 `vendor` 只有 312 K、根本没有这个夹具，所以云端天然不复现。此条从未计入回归数字（上一轮隔离结果里它是 16 个 skip 之外的独立 error，本轮已消除）。

## 9. 云端更正（captain 指正）

t2 里我连的是 `47.243.252.141`（不是本项目主机），因此“云端未实测”的结论作废。正确主机 **47.104.8.84** 本轮已用 `id_ed25519_194` 验证可登录：

- `hostname` = `iZm5e0yfmsms1tm20u2mu3Z`，`companion.service` **active**；
- `/opt/qq-deepseek` **无 `tests/` 目录**，`vendor` 312 K，`/tmp` 余量 31 G；
- `.venv-companion`：`jsonschema`/`aiohttp`/`edge_tts` 有，`websockets`/`numpy` 无。

按 captain 指示本轮**没有向云端写入任何文件**，同步清单见 §6；captain 已明确"测试树同步现在不批"，等 t7 落地后统一部署并连依赖一起核。不私自 `pip install` 补环境。

## 10. skip 不算证据（书面约定）

t3 的 16 个 skip 是 t7 还没实现的契约，不是通过。t8 评审与 t9 回收必须把 skip 单列；任何汇报不得把"841 项 OK"说成"子 agent 框架已验证"——那 16 条恰恰是尚未实现的契约。
