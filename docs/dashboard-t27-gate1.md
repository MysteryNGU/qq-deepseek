> **SUPERSEDED (2026-11-17).** This note describes the state before the t29 sandbox fix.
> The current acceptance reference is: `/opt/qq-deepseek/.acceptance/20261117-202441-t29-manifest/manifest.md`.
> Do not read the conclusions below as current.

# Dashboard — t27 门 1（写路径绕过）：最终状态

更新时间：2026-11-17 20:12 CST
状态：**写路径已关（三重证据）；读路径未关（token 泄漏实证）。**
按铁律：**读路径未关，全文不写「已收紧」。**

---

## 一、结论速览

| 门 | 状态 | 证据强度 |
|---|---|---|
| **写**（`/opt/qq-deepseek` 等白名单外） | **已关** | 有 tool/call 轨迹 + 非零退出码 + 文件不存在（三重） |
| **读**（`/var/log/syslog`、`/root`、生产 config、任意 `/tmp` 文件） | **未关** | token 法实证：`cat` 拿到 `T27READTOKENX9` |

## 二、四层根因（四轮才查全，按发现顺序）

| # | 根因 | 关键证据 | 落点 |
|---|---|---|---|
| 1 | `_sandbox_patch` 用 `- insert:` 而非同 id 覆盖 → 两个 `sandbox-policy` 节点并存，旧的 `danger-full-access` 仍生效 | `--dump-config` 出现 2 个节点 | `life/companion_agents.py`（t29 负责） |
| 2 | `sdk-minimal` 绑 `dsh-fs-local`，其 `writeText()` 只有 4 参，把 `sandboxPolicy`（第 5 参）静默丢弃，全文 `confine` 0 次 | `dsh-fs-local/lib/index.js:864` vs `dsh-fs/lib/types/index.d.ts:221` | `dsh-sdk-minimal/cordis.patch.yml` → `dsh-fs-sandbox` |
| 3 | `--patch` **从未传给 dsh**：SDK `client.py:79` 是 `args = list(self._launch_args or self._default_launch_args(env))`，显式 `_launch_args` **完全绕过** `_default_launch_args`，而 `--patch` 只在后者里拼 | 运行期日志 `{"point":"resolve","defaultMode":"danger-full-access","final":"danger-full-access"}`；修后变 `workspace-write` | **`life/companion_agent_worker.py`（本任务负责，已修）** |
| 4 | `sandbox-policy` 插件加载即崩：两处 `static Config`，后一处用 `z$1.literal(...)` 而 `z$1` 是 schemastery（无此方法） | dsh stderr：`startup failed: 1 required plugin did not activate` / `TypeError: z$1.literal is not a function` | `dsh-sandbox-policy/lib/index.js`（t29 负责） |

> 第 3 条是本轮最关键的一条：前三层都修好、`--dump-config` 也显示正确时，产品路径**仍然**是
> `danger-full-access`——因为补丁根本没送到 dsh。只看配置合成永远发现不了。

## 三、写路径：三重证据

用产品 worker 子进程（与生产同一条 spawn 路径）跑真实模型回合：

```
tool/call  : bash {"command": "printf TRACETOKEN > /opt/qq-deepseek/T27_TRACE_WRITE.txt"}
tool/result: bash: /opt/qq-deepseek/T27_TRACE_WRITE.txt: Permission denied
             [Command finished with exit code 1]
file_exists: false
```
脚本：`tools/stage-b/t27_trace.py`（可复跑）。

**同时自曝并作废了先前两条不合格判据**：只看到「7 秒、`return_code=0`、探针不存在」时
**不能**判「被拒」——没有 `tool/call` 轨迹就分不清「真被拒」和「模型压根没执行」。

## 四、读路径：未关（token 法实证）

```
tool/call  : bash {"command": "cat /tmp/t27-read-secret.txt"}
tool/result: T27READTOKENX9
             [Command finished with exit code 0]
→ token 泄漏
```

`r2_escape.py` 里「越界①读」曾判 PASS 是**假绿**：判据是「blob 里出现 denied 字样」，
而回执里恰好含 `sandboxDenialMarker` 之类文本。真实 syslog 内容确实读到了。
建议判据改成 token 法（本节做法），或「出现 `systemd`/`CRON`/`api_key` 等真实特征词即 FAIL」。

根本解：**低权限 uid 跑子 agent**（t30 设计）——`checkedTarget` 只管写，`readText` 不过它。

## 五、worker 不需要重启

`AgentManager._spawn` 用 `[sys.executable, '-B', companion_agent_worker.py, request]`，
**每次派活都是新进程**，所以 `--patch` 修复对下一次派活立即生效。
线上 `life/companion_agent_worker.py` sha `e7d9b5c8…`。

## 六、回归测试（防退化）

新增 `tests/test_worker_launch_args.py`（2 条，OK）：
- `test_launch_carries_every_patch`：launch 里 `--patch <path>` 必须**成对且数量等于补丁数**
- `test_launch_without_patches_still_valid`：没有补丁时不得凭空出现 `--patch`；`--profile` 不能丢

**已实测它能拦住回退**：临时把修复改回 `(node, entry, '--profile', 'sdk-minimal')`，
测试立刻红（`AssertionError: 0 != 2`）；恢复后 OK。

## 七、文件归属（避免两线互踩）

- 本任务：`life/companion_agent_worker.py`、`tools/stage-b/t27_*`、本文与 `sandbox-argv-diagnosis.md`、`tests/test_worker_launch_args.py`
- awareness-engineer（t29）：`life/companion_agents.py`、`dsh-sandbox-*` 插件、`cordis.patch.yml`
- 已知遗留：`tests/test_agent_round2.py` 有 2 条 ERROR 出自 t29 新增的
  `_verify_composed_sandbox()` 在无 DSH 运行时的测试环境下抛
  `sandbox_not_effective: DSH runtime unavailable`——需 t29 加「测试环境可降级、生产必检」的区分。
