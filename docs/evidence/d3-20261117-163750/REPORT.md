# D3 真实 DSH 委托：一次实跑与失败定位

- 验收者：isolation-verifier（t17，attempt 2）
- 时间：2026-11-17 16:37:50（正式跑）· 16:40–16:42（诊断）
- 授权口径：captain 于 16:35 批准「不做费用强制限制、选择隔离副本 A、空临时目录只读方案」
- 证据：云端 `/opt/qq-deepseek/.acceptance/20261117-163750-d3/`（正式跑）+ `20261117-163557-d3/` `20261117-163643-d3/`（两次干跑）
- **结论：D3 未跑通。链路在生产入口的 4 段里通了 3 段，卡在「DSH 运行时启动」这一行代码上。**
  该失败不花钱（发生在 initialize 之前），且是**确定性的产品缺陷**，不是偶发。

## 0. 一句话

生产入口（模型决策 → 认领 → 台账 → 起子进程）全部真实走通了；断点在
`life/companion_agent_worker.py:106`——它没有把 `_launch_args` 传给 SDK，导致 SDK 去
找从未安装的 `deepseek_harness_runtime` 包并抛 `FileNotFoundError`。
**同一个仓库里 `companion_dsh_worker.py:34` 是对的**，唯独 `node=dsh` 的子 agent 这条路漏了。
修法已验证可用（只到 initialize，不花钱）。

## 1. ① 模拟测试通过（假模型 / 隔离环境已验证的部分）

跑之前先做了两次干跑（`--dry-run`），不执行 DSH、不花钱：

| 项 | 结果 |
|---|---|
| 隔离就位 | `home=/tmp/p10d3-…/home`、`life_db=data/life.db`（副本）、`budget_mode=off`、`dsh_paths=true` |
| 生产入口受理 | `pending`，`kind=agent` |
| 认领进台账 | `_agent_pending_one` → 台账 `queued`、`origin=dsh` |
| 生产台账未写 | 干跑前后 `companion_agent_tasks/events` 均为 0 |

**干跑能覆盖的部分到此为止**：它证明了"从模型决策到台账"这段在隔离环境里已经稳；
它**不能**证明 DSH 真的能启动——这一步只有真跑才暴露，而真跑可以。

## 2. ② 真实链路通过（本次 D3 真正跑通的环节）

命令：`sh tools/stage-c/d3_run.sh real`（工作区 `/tmp/p10d3-20261117-163750`，证据归档同名）

| 验收项 | 判定 | 证据 |
|---|---|---|
| **a. 委托被接收** | **通过** | `(a1)` 决策入口受理：`action_id=companion-action_2935be1f52bc16fab63adae8`、`status=pending`、`kind=agent`；`(a2)` `_agent_pending_one` 认领：`claimed=true`、`action_status=queued`、台账 `agent_id=companion-agent_b2ab770a99c7e2fc4faeda3c`、`task_status=queued`、`origin=dsh` |
| **b. worker 实际执行** | **部分** | `(b1)` 台账执行器 14.0 s 跑完、未超时（预算 300 s）；`(b2)` **抓到真实子进程**：`903519 /opt/qq-deepseek/.venv-companion/bin/python -B /opt/qq-deepseek/life/companion_agent_worker.py …/agent-companion-agent_b2ab770a99c7e2fc4faeda3c.request.json`；**`(b3)` 失败**：`session_id=null`、`finish_reason=null`、日志 `agent worker failed: FileNotFoundError` |
| **c. 结果返回小鱼** | 未成立（由 b 阻断） | 结果轮只收到"失败"这一类回执，不是真实结果 |
| **d. 状态正确落库** | 落库正确，但内容是失败 | 台账落 `failed`，`failure.code=worker_exception`、`reason=FileNotFoundError` |
| **e. 预算记录 / 用量** | 部分 | 这次调用**没有**产生 `companion_usage` 行（DSH 没起来；预算模式 off，`spending_calls` 不新增）；无任何"未知写成 0"——本次没有任何用量被记录，也没编造 |
| **f. stored 不得说成已收到** | 遵守 | 本链路没走到 `send`/`stored`；b/c 的失败按失败记，没有按"已收到"表述 |
| 只读约束 | **通过** | 探测目录跑完仍只有 `probe.txt`，内容未变（`probe_content_changed=false`） |

生产侧核对（前后各一次）：

```
config.toml sha256 : f5e169264efad3563a65ce4f3bccc20d52afee341e57fd6b6c31ca9ff51addb2（前后一致）
service            : active / MainPID=892653 / NRestarts=0 / ActiveEnterTimestamp=16:18:02（未重启）
生产台账           : companion_agent_tasks=0、companion_agent_events=0、agent 类动作=0（前后一致，未写生产库）
/tmp               : 无残留
```

## 3. 失败定位（附代码行与验证）

### 3.1 现象

```
agent worker failed: FileNotFoundError
{"return_code": 1, "failure": {"code": "worker_exception", "reason": "FileNotFoundError"}, "usage": null, "seconds": 0.198}
```

0.198 秒就失败——不是网络问题，是**在起进程那一步就抛了**。

### 3.2 根因

`life/companion_agent_worker.py:106` 是 `harness = DeepSeekHarness(config)`，**没有 `_launch_args`**。

SDK 的默认分支（`vendor/dsh-runtime/python/sdk/src/deepseek_harness/client.py:484-492`）：

```python
def _default_launch_args(self, env):
    if self.config.dsh_bin is None:
        try:
            from deepseek_harness_runtime import resolve_bundled_launch_args
        except ImportError as exc:
            raise FileNotFoundError(
                "Unable to locate the bundled DeepSeek Harness dsh runtime. "
                "Install deepseek-harness-runtime-bin.") from exc
```

而 `deepseek_harness_runtime` **没装**（实测 `ModuleNotFoundError`），于是抛 `FileNotFoundError`，
被 `main()` 的 `except Exception` 折叠成只剩类名（`companion_agent_worker.py:144-147`）。

完整 traceback（`d3_diag_trace.sh` 抓到）：

```
File "…/life/companion_agent_worker.py", line 120, in run_dsh
    result = harness.run(instruction)
  …
File "…/deepseek_harness/client.py", line 80, in start
    self._proc = subprocess.Popen(
FileNotFoundError: [Errno 2] No such file or directory: 'dsh'
```

**对照**：`life/companion_dsh_worker.py:31-34` 正确构造并传入了 `_launch_args`：

```python
launch = (request['node'], str(root/entry), '--profile', 'sdk-minimal')
harness = harness_class(config, _launch_args=launch)
```

即：**主模型那条 DSH 路径能用，子 agent 这条不能用**。这解释了为什么此前所有 DSH 相关验证都"通过"——
它们验的是 `executor` 的 dsh 动作，不是 `node=dsh` 的子 agent。

### 3.3 修法已验证（只读，不花钱）

`tools/stage-c/d3_verify_launch.py`（只到 `harness.start()`，即 initialize 往返，不提交模型请求）：

```
launch args: ('/opt/qq-deepseek/toolchain/node/bin/node',
              '/opt/qq-deepseek/vendor/dsh-runtime/lib/bin.js', '--profile', 'sdk-minimal')
DSH runtime started OK (initialize round-trip succeeded)
```

修法：在 `companion_agent_worker.py:run_dsh()` 里按 `companion_dsh_worker.py` 的写法构造
`launch`，并把 `launch` 传给 `DeepSeekHarness(config, _launch_args=launch)`（1 行改动）。

## 4. ③ 仍未验证

1. **D3 全链路本身**：因为上述缺陷未修，a→b→c→d→e 的完整一次**尚未发生**。修好后需重跑一次。
2. **真实模型在子 agent 里的行为**：会话号、`finish_reason`、能否正确读出探测文件 token、
   是否听话不越权写文件——一个都没验到。
3. **用量与费用**：DSH 不起时连"未知用量记为 null"都无从观察（本次是"没有这一行"而不是"null 行"）。
   修好后要确认 `companion_usage`/回执里 usage 是 `null` 而非 0。
4. **凭据传递**：本次未到需要凭据那一步。已确认生产进程环境里**没有** `DEEPSEEK_API_KEY`、
   `DEEPSEEK_BASE_URL`、`DSH_HOME`（`/proc/892653/environ` 只有 13 个键，无相关项），
   `Spending.reserve()` 也从不给这两个变量赋值。修好 launch 之后**很可能会撞上凭据缺失**
   （`api.py:17-19` 与 `:36-37` 说明：不显式给 `api_key`/`base_url` 就继承调用方环境）。
   这是**下一个可预见的断点**，建议修复时一并处理。
5. **长期稳定性**：即便一次跑通，也只证明基本链路可用；并发、超时恢复、DSH 会话续接、
   多件排队等都没验。

## 5. 状态

- 本次正式跑**只跑了一次**（硬约束 4：不许无限重试）。诊断脚本另行单独跑，不构成"重试"。
- 未改任何产品代码。修法待 captain 指派（我建议由 subagent-engineer 修，我不改产品代码）。
- 诊断脚本：`tools/stage-c/d3_diag_env.sh`、`d3_diag_trace.sh`、`d3_diag_paths.sh`、
  `d3_diag_launch.sh`、`d3_verify_launch.py`（均可复跑，全部只读）。
