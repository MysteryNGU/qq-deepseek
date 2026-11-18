# t11 补充：`node=work` 的 C 方案（`command_required_for_work_node`）

日期：2026-11-18 01:38–01:52 CST　执行：isolation-verifier
范围：**只动 `life/companion_agents.py`**（captain 指定；`companion.py` 的 3 行说明等 t13 落盘后再同步）。

---

## 1. 改了什么

| # | 位置 | 改动 |
|---|---|---|
| 1 | `OP_FIELDS['create']`（:74-76） | 加 `'command'`——不加，`_action_fields` 会在入口把模型写的命令丢掉（这是「只改说明不够」的原因） |
| 2 | `_action_fields`（:2454-2459） | 顶层拾取名单加 `'command'`（模型把 command 写在顶层而不是 arguments 里时也能认） |
| 3 | `create()` 签名（:831-834） | 新增 `command=''` 关键字参数 |
| 4 | `create()` 前置校验（:866-877） | `node=work` 且没有 command → 抛 `command_required_for_work_node` |
| 5 | `create()` 台账（:912-916） | 把 `command` 记进 `checkpoint`——重启/续跑时 `_run_worker` 还要用它 |
| 6 | `_run_worker()`（:1971-1994） | 跑 `action['command']`，其次 `checkpoint['command']`；**不用 goal 兜底**（goal 恰好是 `ls` 就真的 ls 了） |

**错误消息**（按 captain 要求给出可执行的下一步）：

```
node=work 要在 command 里写具体命令（例如 command="pytest -q" 或
command="python build.py"）；只想让它按目标自动把事情做完，用 node=dsh；
新建文件这类小活，用 node=echo。
```

---

## 2. 为什么这个缺陷能活到现在（captain 要求写进报告）

> **现有用例全走 `node=echo/stub`（goal 即脚本），`node=work` 的真实语义从没被「只有 goal」的用例覆盖过。**

今晚这个模式和更早的 `inspect{agent_id}` 是同一族，但更隐蔽：

| | `inspect{agent_id}` | `node=work` + 只有 goal |
|---|---|---|
| 什么时候失败 | **入口**（schema 拒） | **两秒后从台账里读出来** |
| 模型看到什么 | 「你参数写错了」→ 能改 | `command backend needs a command` → 只会觉得「这事做不成」 |
| 单测能不能抓 | 能（走 schema） | **不能**：没有一条用例用「只有 goal 的 work」跑 |

**通用教训**：**「单测全绿」只证明被覆盖的路径对。没被任何用例走过的后端语义，
等于没有被测过——而它恰好是说明里教模型去用的那一条。**

---

## 3. 验证：A/B 对照（同机、同测试集、只差一个模块）

因为 `life/companion.py` 此刻正被 t13 改着（带 `silenced` 未定义的问题），
所有对照都**把 `companion.py` 固定成云端那份**（`837513ab…`），只替换
`companion_agents.py`（云端 `a2123c5e…` vs 我的补丁），其余完全一致：

```
base (cloud module): Ran 86 tests → FAILED (failures=8, errors=3, skipped=5)
mine (patched)     : Ran 86 tests → FAILED (failures=5, errors=2, skipped=5)

failures only in mine（我引入的）: []
failures only in base（我的补丁修好的）:
  test_work_node_without_command_is_refused_before_dispatch
  test_refusal_happens_through_the_real_action_entry
  test_work_node_with_command_is_accepted_and_kept
  test_command_survives_the_op_fields_whitelist
failures in both（与本次改动无关，改动前就有）:
  test_agent_instructions_tell_her_about_command      ← 等 companion.py 的说明同步
  test_third_nesting_level_is_rejected                ← parent_ambiguous（另一处语义）
  test_same_name_different_account_is_not_merged
  test_credential_is_not_in_agent_readable_state
  test_workdir_allowed_but_outside_and_secrets_refused
  test_verification_unavailable_is_not_reported_as_ineffective
  test_off_records_call_with_unknown_cost
```

**结论：我的改动引入 0 个新失败，修好 4 条新回归。** 剩下 7 条在云端模块上同样失败，
与本任务无关；其中 `test_third_nesting_level_is_rejected` 值得单独看一眼（它抛
`parent_ambiguous`，是另一个「用名字算深度」的语义问题），我没在本任务里动它。

> 复现命令：`.acceptance-local/compare_against_baseline.py <unittest targets>`
> ——它会建两个一次性容器（base / mine），只差 `companion_agents.py` 一个文件。

---

## 4. 新增的回归测试（`tests/test_agent_repair.py::WorkNodeNeedsCommand`，6 条全绿）

| 用例 | 钉住什么 |
|---|---|
| `test_work_node_without_command_is_refused_before_dispatch` | 拒绝 + 消息里必须出现 `command`、`node=dsh`、`node=echo` |
| `test_refusal_happens_through_the_real_action_entry` | 走真执行器入口；被拒的活**不许在台账里留一条永远 queued 的记录** |
| `test_work_node_with_command_is_accepted_and_kept` | 带 command 能登记，且 command 进了 `checkpoint` |
| `test_command_survives_the_op_fields_whitelist` | `OP_FIELDS['create']` 必须有 `command`，否则入口就丢 |
| `test_goal_is_never_used_as_a_command` | 读 `_run_worker` 源码：有 `action.get('command')`、**没有** `action.get('goal')` |
| `test_agent_instructions_tell_her_about_command` | 说明与实现对齐（**等 companion.py 同步后转绿**） |

另外 `tests/test_agent_contract.py` 的测试夹具原先是「不写 node → 靠默认值」，
默认值就是 work，如今会被正当拒绝。已显式改成 `node="echo"`——**这不是让测试绕过校验，
而是让契约测试只测它自己要测的东西**（台账/状态机），命令后端该由新的回归用例负责。

---

## 5. 还没做的两件事（等条件）

1. **`companion.py` 的 3 行说明**（node 决定在哪儿干…）——captain 要求等 t13 落盘后再同步，
   否则会和 t13 的重放撞车。同步后 `test_agent_instructions_tell_her_about_command` 会转绿。
2. **部署**——本报告只到「本地改动 + 隔离验证」，没有上云端、没有重启服务。

## 6. 过程中的两处自伤（记下来免得别人再踩）

1. **PowerShell `-replace` 写回中文文件会毁编码**：我用 `Get-Content -Raw | Set-Content`
   改 `tests/test_agent_repair.py`，整个文件的中文变成乱码（58 行损坏）。已用 `git checkout`
   恢复、改回 edit 工具。**规矩：中文源文件只用 edit 工具改，不用 shell 重写。**
2. 顺带发现：本任务期间 **`life/companion.py` 被并行改动**过（t13 正在重放），带一个
   `NameError: silenced`。我的 A/B 对照全程用云端那份 `837513ab…`，不受它影响；
   但**任何现在跑全量测试的人都会撞上它**，建议 t13 收尾时先确认这个。
