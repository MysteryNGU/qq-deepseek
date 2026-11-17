# 小鱼持续认知/子 agent 闭环 — 隔离测试机制

日期：2026-11-17。状态：P0 隔离测试机制与基线回归（task t2）。历史报告里的测试数字一律不作为本轮结论；本文每个数字都来自本轮可复现命令。

## 0. 结论先行

| 项目 | 结果 |
| --- | --- |
| 隔离机制 | `tests/companion_test_isolation.py` + `tests/run_isolated_suite.py`，只依赖标准库，自检 6 项全 pass |
| 隔离 `drivetest.py` | **通过 42，失败 0**，exit 0，2.8 s（临时 HOME + 源码副本 + 全新临时库） |
| 原地 `unittest discover`（参照） | 816 项 OK；加入 t3 的 25 项后 841 项 OK，原地 257.99 s |
| 隔离 `unittest discover` | **841 项 OK（skipped=16）**，`FAILURE_COUNT=0`，310.3 s；见 3.5 与 3.6 |
| `run_offline_suite.py` 180 s 硬超时 | 本机必误报；替代命令见 3.4 |
| 云端（`/opt/qq-deepseek`） | **尚未在云端跑测试**；正确主机 47.104.8.84（t2 误连 47.243.252.141），693 项旧说法既不证实也不推翻（见 6） |

## 1. 为什么必须隔离

- 云端 `/opt/qq-deepseek` **没有 git**，同时又是**生产运行目录**：`life/runtime.py`、`life/release.py` 等会在原地建库、写日志、跑发布套件。
- 生产状态在 `/var/lib/companion/xiaoyu`（`life.db` 等），生产配置 `config.toml` 含 `ws_token`、`api_key`、QQ 号与本机绝对路径。
- 现有 `tests/run_offline_suite.py` 名义上“offline”，实际行为是：
  - 在**当前工作目录原地**跑 `python -m unittest discover -s tests` 和 `drivetest.py`；
  - 外层 `subprocess.run(..., timeout=180)` 是**硬超时**，超时即判失败；
  - 只设了 `QQ_OFFLINE=1`，没有独立 HOME、没有独立数据库、没有网络/子进程拦截。

所以本轮不新增任何“在 `/opt/qq-deepseek` 原地跑写库用例”的做法：**测试只在 `/tmp`（本机 `%TEMP%`）下的副本里跑**。

## 2. 可复用隔离机制

### 2.1 沙箱做了什么

1. **路径改写到临时 HOME**：`HOME`、`USERPROFILE`、`APPDATA`、`LOCALAPPDATA`、`XDG_CONFIG_HOME`/`XDG_CACHE_HOME`/`XDG_DATA_HOME`/`XDG_STATE_HOME`、`TMPDIR`/`TEMP`/`TMP` 全部指向 `companion-test-<时间>-<随机>/home|tmp`，`Path.home()` 与 `tempfile` 不再落在操作员账号。
2. **数据库**：
   - `--db fresh`（默认）：沙箱内新建空 `data/life.db`；
   - `--db copy`：用 SQLite backup API 从 `mode=ro` 只读连接复制（避免直接 `cp` 拿到半写状态），复制后 `chmod 0444`，并在复制前后对**源文件**取 sha256 断言未变化。
3. **源码副本**：robocopy（Windows，比 copytree 快约 100 倍）/`shutil.copytree`（POSIX）复制成 `tree/`，跳过 `.git`、`.venv*`、`.test-deps`、`data`、`vendor`、`napcat*`、`local-backups`、`__pycache__`、迁移目录等；实测 642 文件 / 7.1 MB / 2.9 s。副本里的 `config.toml` 会把 `"ws_token"`、`"api_key"` 置空（原文件不动）。
4. **外部平台与外部执行禁用**：**删除**凭据类变量（`DEEPSEEK_API_KEY`、`COMPANION_API_KEY`、`OPENAI_API_KEY`、`ANTHROPIC_API_KEY`、`AWS_ACCESS_KEY_ID`、`AWS_SECRET_ACCESS_KEY`、`HF_TOKEN`、`QQ_WS_URL`、`QQ_WORKER_TOKEN`、`QQ_WORKER_HEALTH`、`ADB_PATH`、`ANDROID_SERIAL`、`OPENAI_BASE_URL`），**保留**防护开关（`QQ_OFFLINE=1`、`HF_HUB_OFFLINE=1`、`HF_HUB_DISABLE_TELEMETRY=1`、`TRANSFORMERS_OFFLINE=1`、`CUDA_VISIBLE_DEVICES=""`）；并提供进程内守卫 `network_guard()`（只放行 127.0.0.1/::1）与 `external_exec_guard()`（拦截 `subprocess.Popen`）。为什么密钥要删除而不是置空见第 8 节。
5. **清理**：`cleanup()` 幂等，第二次调用也打印 `cleaned=` 与 `home_deleted=true`；Windows 走 `\\?\` 扩展路径 + 去只读的 `force_delete()`，POSIX 走 rmtree。
6. **生产路径守卫**：沙箱父目录落在受保护路径内（`/var/lib/companion`、`/opt/qq-deepseek/life`、`/opt/qq-deepseek/tests`、`/opt/qq-deepseek/.venv-companion`，可用 `COMPANION_PROTECTED_ROOTS` 追加）时直接拒绝创建。

### 2.2 命令

本机（源码副本 `C:\Users\76328\qq-deepseek`）：

```powershell
$env:PYTHONUTF8='1'
python -B -X utf8 tests\run_isolated_suite.py --self-check-only      # 只验证隔离，不跑用例
python -B -X utf8 tests\run_isolated_suite.py --timeout 900 --log-dir "$env:TEMP\isolated-logs"
python -B -X utf8 tests\run_isolated_suite.py --only drivetest --timeout 300
python -B -X utf8 tests\run_isolated_suite.py --db copy --db-source data\life.db --only unittest
```

云端（`/opt/qq-deepseek` 无 git，一律在 `/tmp` 副本里跑；本机执行前需先同步这两个脚本，同步属于部署动作，需用户确认）：

```bash
cd /opt/qq-deepseek
/opt/qq-deepseek/.venv-companion/bin/python -B -X utf8 tests/companion_test_isolation.py \
  --snapshot-manifest /tmp/companion-baseline-$(date +%Y%m%d).sha256 \
  --snapshot-exclude __pycache__ .git data vendor napcat
/opt/qq-deepseek/.venv-companion/bin/python -B -X utf8 tests/run_isolated_suite.py \
  --db copy --db-source /var/lib/companion/xiaoyu/life.db --no-deps --timeout 900 \
  --log-dir /tmp/isolated-logs
```

输出约定：`STEP <名字> exit=<码> seconds=<秒>`、每步末尾 8 行、`TREE_CLEAN=<bool>`（原始源码树是否多出新文件）、`FAILURE_COUNT=`+逐条 `ERROR/FAIL`、`TOTAL_SECONDS=`，最后**一定**打印 `cleaned=` 与 `home_deleted=`。

依赖：本机用 `.test-deps`（脚本默认加进 `PYTHONPATH`，可用 `--deps` 追加以 `--no-deps` 关闭）；云端用 `.venv-companion` 自带依赖，加 `--no-deps`。

## 3. 基线回归（本机实测）

### 3.1 隔离自检

```
$ python -B -X utf8 tests\run_isolated_suite.py --self-check-only
# sandbox=C:\Users\76328\AppData\Local\Temp\companion-test-20261117-084705-facf24
# home=...\home db_mode=fresh db_sha256=e3b0c442...b855
self_check home_is_sandbox=pass
self_check life_db_sha_matches=pass
self_check network_guard_blocks=pass
self_check external_exec_blocked=pass
self_check deny_env_applied=pass
cleaned=...companion-test-20261117-084705-facf24 ok=true
home_deleted=true
```

`--db copy --db-source data\life.db --copy-source .` 自检同样全 pass，且 `db_source_sha256_before = db_source_sha256_after = 45747bb6…babf36`，证明生产库副本未被写入。

### 3.2 隔离沙箱内的 `drivetest.py`

```
$ python -B -X utf8 tests\run_isolated_suite.py --only drivetest --timeout 300
STEP drivetest exit=0 seconds=2.8
  |   ok   I3 重启后高位/恢复状态保存
  |   ok   I4 重启后不重复结算
  | 通过 42，失败 0
TREE_CLEAN=true
TOTAL_SECONDS=2.8
  drivetest=2.8s
cleaned=...companion-test-20261117-085341-166592 ok=true
home_deleted=true
```

### 3.3 参照：原地运行（不是验收命令，只用来对比耗时）

```
$env:PYTHONPATH='.test-deps'; $env:PYTHONUTF8='1'; $env:QQ_OFFLINE='1'
python -B -X utf8 -m unittest discover -s tests -v
...
Ran 816 tests in 390.807s
OK           （exit 0）

$env:PYTHONUTF8='1'; python -B -X utf8 drivetest.py
通过 42，失败 0    （exit 0，3.4 s）
```

任务说明里的“816 项通过 253 秒”是更早的机器状态；本轮 `-v` 实测 390.8 s。

### 3.4 `tests/run_offline_suite.py` 的 180 秒硬超时

`run_offline_suite.py` 第 18-19 行：

```python
result = subprocess.run([sys.executable,'-B','-X','utf8',*arguments],cwd=ROOT,env=env,
                        capture_output=True,timeout=180)
```

本机 `unittest discover` 需要约 390 s，远超 180 s，所以本机运行时外层必然抛 `subprocess.TimeoutExpired`，表现为“套件失败”。这是外层超时误报，不是用例失败。

替代命令（等价覆盖、分别判读）：

```powershell
$env:PYTHONPATH='.test-deps'; $env:PYTHONUTF8='1'; $env:QQ_OFFLINE='1'; $env:PYTHONIOENCODING='utf-8'
python -B -X utf8 -m unittest discover -s tests -q      # 期望 exit 0
python -B -X utf8 drivetest.py                          # 期望“通过 42，失败 0”且 exit 0
```

推荐用本轮 runner：`python -B -X utf8 tests\run_isolated_suite.py --timeout 900`。`--only offline_suite` 仍可原样调用旧脚本复现该误报，但不作为验收依据。是否把 180 s 改成可配置（默认保留，避免放宽发布门禁）由 captain 决定，本轮未改该文件。

### 3.5 隔离沙箱内的 `unittest discover`：816 项，4 个失败（如实记录）

```
STEP unittest exit=1 seconds=393.2
  | Ran 816 tests in 386.392s
  | FAILED (failures=3, errors=1)
FAILURE_COUNT=4
  ! ERROR: test_contract_fixture_is_generated_from_the_python_source_and_rejects_bad_fields (tests.test_context_efficiency.ContextEfficiencyTests...)
  ! FAIL: test_authenticated_save_and_reload_preserve_secret (tests.test_settings.SettingsHttpTests...)
  ! FAIL: test_valid_save_roundtrips_and_keeps_masked_secrets (tests.test_settings.SettingsHttpTests...)
  ! FAIL: test_valid_save_roundtrips_and_keeps_masked_secrets (tests.test_settings.SettingsTests...)
```

原因（来自日志原文）：

1. **settings ×3**：`AssertionError: '' != 'private-test-key'`。**已定位并结案为隔离假失败**（见 3.6 与 8）：`DENY_ENV` 曾把 `DEEPSEEK_API_KEY` 写成空字符串，而 `load_config` 把"变量存在"当生效；改成删除该变量后转绿。
2. **context_efficiency ×1**：`FileNotFoundError: ...\tree\vendor\dsh-bridge-work\packages\sdk\server\tests\fixtures\qq-life-contracts.json`。该用例直接读取 `vendor/dsh-bridge-work/`（本地 2.1 GB、含数十万嵌套 `node_modules` 文件）。**本地沙箱假失败，云端不适用**：云端 vendor 只有 312 K、没有该夹具，天然不复现。沙箱随后改为按 `VENDOR_INCLUDE` 只复制夹具文件，本轮重跑已通过。

### 3.5.1 隔离环境下该跑哪些依赖（实测两条路径）

- **带 `.test-deps`**（脚本默认）：本轮 841 项全绿（`skipped=16`）。
- **不带依赖目录**（`--no-deps`）：早期实测 7 个测试模块 `ImportError: No module named 'jsonschema'` 等，另有 60+ 用例连带失败。说明 `.test-deps` 是必需的，云端应改用 `.venv-companion` 里的真实依赖而不是去掉依赖（云端缺 `websockets`/`numpy`，见第 6 节）。

### 3.6 修复后重跑（结案证据）

```
$ python -B -X utf8 tests\run_isolated_suite.py --only unittest --timeout 900
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

`skipped=16` 是 t3 新增的、等待 t7 实现的契约用例（见 `docs/companion-agent-contract-tests.md`），**不算通过**。

## 4. 已知限制（来自本轮实测）

1. **Windows 长路径树删不干净**：早期版本把整棵 `vendor`（嵌套 `node_modules`，路径 >260 字符）复制进沙箱后，`cleanup()` 反复报 `ok=false`，并把 `robocopy`/`rmdir` 卡住。同一批文件在**无争用**时用 `\\?\` 扩展路径删除可以完成数十次通过；在有其它复制任务争用时会反复失败（每轮都只多删掉一部分）。已改成**不复制整棵 vendor**（只按 `VENDOR_INCLUDE` 复制夹具）并加 `force_delete()`；相关实验沙箱已由 captain 代为删除。
2. **网络/子进程守卫只覆盖进程内**：`run_isolated_suite.py` 拉起的子进程靠环境变量禁用（`QQ_OFFLINE=1`、删除密钥变量），没有 OS 级拦截；要更硬的边界应放到容器或微虚拟机里跑。
3. **`unittest discover` 会导入 `tests/`，但不会执行 `tests/manual/`**：真实 QQ、真机、真实模型的验收仍须人工在隔离账号上做，不计入本文通过。
4. 隔离机制不改变任何既有断言：失败照原样失败，不跳用例。

## 5. 清理回执与遗留（如实）

正常运行路径都会打印回执，例如：

```
cleaned=C:\Users\76328\AppData\Local\Temp\companion-test-20261117-085341-166592 ok=true
home_deleted=true
```

本轮结束时机器上仍有实验沙箱未删净（均为我自己实验产生，不含生产数据，也不在项目目录内）：

| 目录 | 来源 | 状态 |
| --- | --- | --- |
| `%TEMP%\companion-test-20261117-083144-9eff21` | 早期整棵 vendor 复制实验（路径 >260 字符的嵌套 `node_modules`） | **已由 captain 代为删除**（删除前核对过路径确在 TEMP 下） |
| `%TEMP%\companion-test-20261117-091942-2f60c0` | 自检在清理阶段中断 | 已删除（`exists=False`） |
| `%TEMP%\companion-test-20261117-093435-7b2c80` | vendor 裁剪实验 | 已删除（`exists=False`） |

删除建议（供后续参考）：关掉其它大文件复制任务后执行 `Remove-Item -LiteralPath <目录> -Recurse -Force`；若仍报“目录不为空”，用 `\\?\` 前缀逐项删除。

## 6. 云端基线：主机更正，尚未在云端跑测试

> **更正（2026-11-17）**：t2 里我连的是 `47.243.252.141`，**不是本项目主机**；captain 指正正确主机是 **47.104.8.84**。因此下面“凭据缺失”的判断作废——真正原因是连错主机，不是没凭据。

正确主机已用 `ssh -i ~/.ssh/id_ed25519_194 root@47.104.8.84` 只读验证：

- `hostname` = `iZm5e0yfmsms1tm20u2mu3Z`，`companion.service` **active**；
- `/opt/qq-deepseek` **没有 `tests/` 目录**；`vendor` 只有 312 K（本地 2.1 GB 是本地特有）；`/tmp` 余量 31 G；
- `.venv-companion` 有 `jsonschema`/`aiohttp`/`edge_tts`，**缺 `websockets`/`numpy`**。

现状与纪律：

- 云端 `693 项` 的旧说法仍未重测，**既未证实也未推翻**；不得写成当前基线。
- cloud 测试树同步**当前不批**（captain 决定）：`.venv-companion` 缺 `websockets`/`numpy`，直接同步会有 `ImportError`；等 t7 落地后由 captain 统一部署并连依赖一起核。清单与 `--no-deps` 命令见 `docs/companion-agent-contract-tests.md` §6。
- 不私自 `pip install` 补环境；本轮**没有**向云端写入任何文件，也没有触碰生产 `life.db` 与 `config.toml`。
- 历史记录（作废）：t2 时用错了主机，两次 `ssh` 都返回
  `root@47.243.252.141: Permission denied (publickey,password)`。

## 7. 复现清单

| 项目 | 值 |
| --- | --- |
| 本机解释器 | `D:\python3\python.exe` 3.12.3 |
| 依赖目录 | `.test-deps`（脚本自动加入 PYTHONPATH） |
| 原地 unittest（-v） | 816 项，390.807 s，OK |
| 原地 drivetest | 通过 42，失败 0，3.4 s |
| 隔离 drivetest | 通过 42，失败 0，2.8 s |
| 隔离 unittest | 841 项 OK（skipped=16），310.3 s，FAILURE_COUNT=0（见 3.6） |
| 源码副本规模 | 642 文件 / 7.1 MB / 2.9 s |
| 生产库副本校验 | `data\life.db` sha256 `45747bb6…babf36` 复制前后一致 |
| 自检项 | home_is_sandbox / life_db_sha_matches / network_guard_blocks / external_exec_blocked / secret_env_absent / switch_env_present |
| 云端 | 正确主机 47.104.8.84（t2 误连 47.243.252.141），尚未在云端跑测试；见 6 |

## 8. 2026-11-17 更新：密钥类变量改为 del，隔离套件转绿

captain 决定：`DENY_ENV` 里的**密钥/凭据类**改成从环境里删除，**防护开关类**保留。改法（`tests/companion_test_isolation.py`）：

| 类别 | 变量 | 处理 |
| --- | --- | --- |
| 密钥/凭据 | `DEEPSEEK_API_KEY`、`COMPANION_API_KEY`、`OPENAI_API_KEY`、`ANTHROPIC_API_KEY`、`AWS_ACCESS_KEY_ID`、`AWS_SECRET_ACCESS_KEY`、`HF_TOKEN`、`QQ_WS_URL`、`QQ_WORKER_TOKEN`、`QQ_WORKER_HEALTH`、`ADB_PATH`、`ANDROID_SERIAL`、`OPENAI_BASE_URL` | `SECRET_ENV`，`env()` 里 `pop` 删除 |
| 防护开关 | `QQ_OFFLINE=1`、`HF_HUB_OFFLINE=1`、`HF_HUB_DISABLE_TELEMETRY=1`、`TRANSFORMERS_OFFLINE=1`、`CUDA_VISIBLE_DEVICES=""` | 保留在 `DENY_ENV` |

自检同步改：原 `deny_env_applied`（断言 `DEEPSEEK_API_KEY == ""`）删除，换成
`secret_env_absent`（密钥键确实不存在）与 `switch_env_present`（防护开关值与 `QQ_OFFLINE=1` 仍在），否则改 del 后自检自己会挂。

结果：settings×3 **转绿，结案为隔离假失败**；context_efficiency×1 也不再出现（沙箱按 `VENDOR_INCLUDE` 只复制 vendor 夹具，用例拿到输入）。隔离全量：`Ran 841 tests in 304.564s / OK (skipped=16)`，`FAILURE_COUNT=0`，`cleaned ok=true`，`home_deleted=true`。

注意：16 个 skip 是 `docs/companion-agent-contract-tests.md` 里等待 t7 实现的契约，**不算通过**，任何汇报不得把 841 OK 说成"子 agent 框架已验证"。
