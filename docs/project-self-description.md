# 项目参考地图：qq-deepseek 自身

面向对象：需要改动这个项目的人或 agent。这是**参考地图**，不是教程。
它回答四件事：代码在哪、每个模块管什么、改完怎样才算真的上线、怎么核对这张地图还没过期。

**本文件不写死任何会随时间变化的数字**（行数、文件数、测试数、快照体积、签名指纹）。
凡是这类事实，一律给查询命令，写的时候现查。

---

## 0. 这张地图自己怎么被校验

本文件依赖的对象只有「路径」和「文件里出现的符号 / 字面量」。校验脚本不解析散文，
它逐条检查下面两类断言，缺一条或多一条都会失败：

- **路径存在**：清单里的每一条路径，在给定根目录下必须存在。
- **符号存在**：清单 `symbols` 里给出的每个字面量，必须能在对应文件中找到。
- **同步**：正文里提到的源码路径必须都在清单里，清单里的路径也必须都在正文里出现
  （后者用来抓「文档改了、清单没删」的半更新状态）。

本文件里的 `SYM(...)` 指的就是清单 `symbols` 下的条目；清单本身是唯一事实源，
散文只解释它。

### 0.1 文档里内嵌一份机器可读清单

清单就是下面这个代码块的全部内容。改文档时连它一起改，两者不一致校验会报出来
（校验要求「文中提到的路径 ∈ 清单」，所以清单不是摆设）。

<!-- doccheck:begin -->

```json
{
  "paths": [
    "companion.py",
    "config.example.toml",
    "README.md",
    "requirements-companion.txt",
    "deploy/companion/companion.service",
    "ops/companion-watchdog.py",
    "ops/companion-watchdog.service",
    "tools/deploy-companion-install.sh",
    "tools/cloud-build-publish.sh",
    "tools/build-companion-android.py",
    "tests/companion_test_isolation.py",
    "tests/run_isolated_module.py",
    "tests/run_isolated_suite.py",
    "tests/run_offline_suite.py",
    "drivetest.py",
    "life/__init__.py",
    "life/companion.py",
    "life/companion_awareness.py",
    "life/companion_agents.py",
    "life/companion_executor.py",
    "life/companion_remote.py",
    "life/companion_qq.py",
    "life/companion_mobile.py",
    "life/companion_desktop.py",
    "life/companion_context.py",
    "life/companion_im.py",
    "life/role_host.py",
    "docs/companion-watchdog.md",
    "docs/companion-test-isolation.md",
    "docs/cloud-sync-policy.md",
    "docs/companion-cloud-android-build.md"
  ],
  "symbols": {
    "deploy/companion/companion.service": [
      "WorkingDirectory=/opt/qq-deepseek",
      "User=companion",
      "--home /var/lib/companion/xiaoyu",
      "--serve",
      "--port 8765",
      "Restart=on-failure"
    ],
    "ops/companion-watchdog.service": [
      "/opt/qq-deepseek/ops/companion-watchdog.py",
      "User=root"
    ],
    "ops/companion-watchdog.py": [
      "source_root='/opt/qq-deepseek'",
      "state_root='/var/lib/companion/xiaoyu/data/watchdog'",
      "unit='companion.service'",
      "health_url='http://127.0.0.1:8765/alive'",
      "max_failures=3",
      "window_seconds=",
      "'toolchain'",
      "'vendor'",
      "'config.toml'"
    ],
    "life/companion_remote.py": [
      "add_get('/alive'",
      "add_get('/health'",
      "def alive",
      "def alive(self, request)",
      "path=='/alive'"
    ],
    "life/companion.py": [
      "class Companion:",
      "available_actions=kinds"
    ],
    "life/companion_awareness.py": [
      "def capabilities(",
      "def build(",
      "def agent_capability(",
      "SCHEMA_VERSION"
    ],
    "life/companion_agents.py": [
      "class AgentManager",
      "def _agent_paths",
      "/opt/qq-deepseek/toolchain"
    ],
    "life/companion_executor.py": [
      "class ActionExecutor",
      "def available_kinds"
    ],
    "life/companion_context.py": [
      "op=awareness/capabilities/attention/agent_tasks"
    ],
    "tests/companion_test_isolation.py": [
      "/var/lib/companion"
    ],
    "life/__init__.py": [
      "from .runtime import LifeRuntime"
    ]
  }
}
```

<!-- doccheck:end -->

### 0.2 校验命令

**本机（Git 仓库根目录）**：

```powershell
@'
import io, json, re, sys
from pathlib import Path
doc = Path('docs/project-self-description.md')
text = io.open(doc, encoding='utf-8').read()
block = re.search(r'<!-- doccheck:begin -->\n\n```json\n(.*?)\n```\n\n<!-- doccheck:end -->',
                  text, re.S)
manifest = json.loads(block.group(1))
listed = set(manifest['paths'])
# 文中提到的源码路径：清单里的条目出现在正文任何位置都算（含命令、表格、代码块）
mentioned = set(re.findall(r'((?:life|ops|tools|tests|deploy|mobile|docs)/[\w./-]+\.'
                           r'(?:py|md|service|sh)|(?:companion|drivetest|config\.example)\.'
                           r'(?:py|toml)|requirements-[\w-]+\.txt|README\.md)', text))
problems = []
for path in sorted(listed):
    if not Path(path).is_file():
        problems.append('MISSING PATH %s' % path)
for path, needles in manifest['symbols'].items():
    try:
        body = io.open(path, encoding='utf-8').read()
    except OSError as error:
        problems.append('UNREADABLE %s (%s)' % (path, error)); continue
    for needle in needles:
        if needle not in body:
            problems.append('MISSING SYMBOL %s :: %s' % (path, needle))
for path in sorted(mentioned - listed - {str(doc).replace('\\', '/')}):
    problems.append('UNLISTED PATH IN PROSE %s' % path)
for path in sorted(listed - mentioned):
    problems.append('LISTED BUT NOT MENTIONED %s' % path)
print('CHECKS=%d PROBLEMS=%d' % (len(listed) + sum(len(v) for v in manifest['symbols'].values()),
                                 len(problems)))
for line in problems: print('  ! ' + line)
sys.exit(1 if problems else 0)
'@ | Set-Content -Encoding utf8 $env:TEMP\doccheck.py
python -B -X utf8 $env:TEMP\doccheck.py; "exit=$LASTEXITCODE"
```

期望输出最后一行 `PROBLEMS=0` 且退出码 0。「LISTED BUT NOT MENTIONED」不是错误判据本身，
但它能抓出「文档改了、清单没删」的半更新状态，比沉默地过期好。

**云端（生产源码，无 git）**：把同一个 `@'…'@` 块原样粘贴到 bash 里不行（`@'` 是
PowerShell 的 here-string）。云端改用 heredoc，读取的还是同一份清单和同一套判据：

```bash
cd /opt/qq-deepseek && /opt/qq-deepseek/.venv-companion/bin/python -B -X utf8 /dev/stdin <<'PY'
import io, json, re, sys
from pathlib import Path
doc = Path('docs/project-self-description.md')
text = io.open(doc, encoding='utf-8').read()
block = re.search(r'<!-- doccheck:begin -->\n\n```json\n(.*?)\n```\n\n<!-- doccheck:end -->',
                  text, re.S)
manifest = json.loads(block.group(1))
listed = set(manifest['paths'])
mentioned = set(re.findall(r'((?:life|ops|tools|tests|deploy|mobile|docs)/[\w./-]+\.'
                           r'(?:py|md|service|sh)|(?:companion|drivetest|config\.example)\.'
                           r'(?:py|toml)|requirements-[\w-]+\.txt|README\.md)', text))
problems = []
for path in sorted(listed):
    if not Path(path).is_file():
        problems.append('MISSING PATH %s' % path)
for path, needles in manifest['symbols'].items():
    try:
        body = io.open(path, encoding='utf-8').read()
    except OSError as error:
        problems.append('UNREADABLE %s (%s)' % (path, error)); continue
    for needle in needles:
        if needle not in body:
            problems.append('MISSING SYMBOL %s :: %s' % (path, needle))
for path in sorted(mentioned - listed - {str(doc), 'docs/project-self-description.md'}):
    problems.append('UNLISTED PATH IN PROSE %s' % path)
for path in sorted(listed - mentioned):
    problems.append('LISTED BUT NOT MENTIONED %s' % path)
print('CHECKS=%d PROBLEMS=%d' % (len(listed) + sum(len(v) for v in manifest['symbols'].values()),
                                 len(problems)))
for line in problems: print('  ! ' + line)
sys.exit(1 if problems else 0)
PY
```

在云端跑出 `PROBLEMS=0` 只说明「这份文档与**云端那份源码**一致」，不说明「云端和本仓库一致」。
后者要比哈希：

```bash
# 云端
cd /opt/qq-deepseek && find life ops deploy tools tests companion.py drivetest.py -name '*.py' \
  -o -name '*.service' -o -name '*.sh' | sort | xargs sha256sum > /tmp/cloud-src.sha256
```

把 `/tmp/cloud-src.sha256` 取回本机，在仓库根目录用同一组路径重算并 `diff`；空 diff 才算同步。

### 0.3 什么时候必须重跑

- 改动 `life/`、`ops/`、`deploy/`、`tools/`、`tests/` 下的**文件路径或文件名**（重命名、移动、删除）。
- 改动 `companion.service` / `companion-watchdog.service` 的启动命令、端口、路径。
- 改动 watchdog 的 `DEFAULTS`（单元名、健康探针 URL、状态目录、失败阈值）。
- 任何一次「云端源码和本仓库不一致」的同步之后。

改动会让本文件失真的，不止上面几条：`§0.1` 清单里没列、但正文里当成事实写下来的
**行为描述**（例如 watchdog 的判定顺序、`_agent_paths()` 的白名单内容），源码一改就必须回来改。
校验能抓路径和字面量，抓不到「描述已经不对了」——那部分靠人。

---

## 1. 两处源码：哪边是真的

| | 生产源码 | Git 仓库 |
|---|---|---|
| 位置 | 云端 `/opt/qq-deepseek` | `C:\Users\76328\qq-deepseek`（origin: `github.com/MysteryNGU/qq-deepseek.git`，分支 `main`） |
| 版本控制 | **没有 `.git`** | 有 |
| 谁在跑 | `companion.service` 从这里启动 | 不直接运行 |
| 数据 | 不在源码树里，在 `/var/lib/companion/xiaoyu` | 本机 `data/`，`.gitignore` 排除 |

**没有「git pull 就发布」这条路。** 云端没有版本库，所以：

- 云端文件改了什么，只能用**文件哈希清单**回答，不能用 `git status` 或 `git log`。
- 两边是否一致，只能逐文件比我方仓库和云端同名文件的 sha256。
- 云端文件的「当前版本」= 云端文件的哈希 + 服务启动时间，两者一起报才有意义。

### 1.1 当前运行版本怎么查

「版本」在这里不是一个数字，而是四件事。任何一条缺了都不算查过。

```bash
# 1) 哪个单元、什么状态、什么时候起来的、跑的是哪个二进制
systemctl show companion.service -p ActiveState -p SubState -p ExecMainStartTimestamp \
  -p MainPID -p ExecStart

# 2) 实际进程与命令行（ExecStart 是配置，这里才是真在跑的）
tr '\0' ' ' < /proc/$(systemctl show -p MainPID --value companion.service)/cmdline; echo

# 3) 源码自称的版本 / 契约版本（来自源码，不是文档）
grep -n 'PROJECTION_VERSION\|SCHEMA_VERSION' /opt/qq-deepseek/life/companion_awareness.py

# 4) 关键模块的哈希（用于和仓库比，也用于 watchdog 快照对账）
sha256sum /opt/qq-deepseek/companion.py /opt/qq-deepseek/life/companion.py \
          /opt/qq-deepseek/life/companion_awareness.py /opt/qq-deepseek/life/companion_agents.py \
          /opt/qq-deepseek/life/companion_executor.py /opt/qq-deepseek/life/companion_remote.py
```

启动时间与哈希要一起报：**服务没重启，改过的文件就没生效**（Python 已 import 的模块不会热加载）。

---

## 2. 模块职责地图

### 2.1 进程与入口

| 路径 | 职责 | 要点 |
|---|---|---|
| `companion.py` | 入口：解析 `--home` / `--serve --host --port`，起服务和 `Companion` | 生产启动命令见 `deploy/companion/companion.service` 的 `ExecStart` |
| `deploy/companion/companion.service` | 生产 systemd 单元 | `User=companion`、`WorkingDirectory=/opt/qq-deepseek`、`Restart=on-failure` |
| `life/role_host.py` | 宿主生命周期（桌面/控制台/调度共用），与 `companion.py` 同层 | Windows 本机走这一层 |
| `config.example.toml` | 配置模板；**真实 `config.toml` 不同步、不提交** | 见 §5 |

### 2.2 主角色与认知层

| 路径 | 职责 | 要点 |
|---|---|---|
| `life/companion.py` | **主角色**。`class Companion` 持有一切：模型、记忆仓储、驱动力、连续性、执行器、子 agent 台账 | 一轮决策→Python 提交，命令在认知锁外跑 |
| `life/companion_awareness.py` | **认知投影**（只读）：我在哪、用谁的号、关注什么、手上几件事、真能用什么 | `capabilities()` 产能力目录；`ready` 只给有真实调用证据的；`needs_setup` 必带原因 |
| `life/companion_context.py` | 提示词投影（工具使用说明 `GUIDES`） | 工具怎么用写在这里，不靠模型记忆 |
| `life/companion_agents.py` | **子 agent**：七操作 + 状态机 + 台账 `companion_agent_tasks` + 白名单 | `class AgentManager`；沙箱只约束它，不约束本体（§5） |
| `life/companion_executor.py` | **执行器**：`class ActionExecutor`，按 `kind` 分派 | `available_kinds()` 是「这台机器现在能干什么」的唯一来源；经 `RemoteExecutor` 包一层再交给角色 |

`available_kinds()` 的结果会原样进主上下文的 `available_actions`（`life/companion.py` 里
`available_actions=kinds` 那行），能力目录再按它生成 `action.<kind>` 条目。所以
**「我能做什么」这条链是：执行器 → available_actions → 能力目录 → 提示词摘要 → 她的话**。
链上任何一环编造，她就会说出没做过的事；查问题先查执行器返回了什么。

### 2.3 传输与渠道

| 路径 | 职责 | 要点 |
|---|---|---|
| `life/companion_remote.py` | **HTTP 服务本体**：`CompanionServer` 持 aiohttp app 与鉴权中间件；`RemoteExecutor` 把 `computer` / `phone` 两个 kind 交给服务端 | 路由：`/health`（要 token）、`/alive`（免 token 存活探针）、`/mobile/*`（手机鉴权）、`/rpc`、`/ws` |
| `life/companion_qq.py` | QQ 通道：OneBot 正向 WebSocket（NapCat 个人号） | 只管收发改走 `core.chat`，不另起认知 |
| `life/companion_mobile.py` | 手机端 API（配对、订阅、投递） | 没有独立认知循环；手机断连影响的是投递目标与 `phone` kind |
| `life/companion_desktop.py` | 桌面原生界面；Companion 与数据库同属一个 asyncio 线程 | 桌面在线与否由 `CompanionServer.peer` 表示 |
| `life/companion_im.py` | IM 总览：QQ 与微信的配置与生命周期 | 渠道切换不换人格、记忆、项目 |

`/alive` 的意义：它只回 `{"alive": true, "pid": N}`，没有状态、没有 token、没有主人数据，
专供外部 watchdog 判断「进程还在服务」用。免鉴权豁免必须写在 `CompanionServer.auth`
中间件里（`path=='/alive'` 那段），写在 `MobileAPI.authenticate` 里不起作用——`/alive`
根本走不到手机鉴权那一层。

### 2.4 自愈

| 路径 | 职责 |
|---|---|
| `ops/companion-watchdog.py` | 独立进程：观察 restart 窗口 → 判健康 → 失败时还原已验证快照 → 重启 → 往 `life.db` 写事件告诉她 |
| `ops/companion-watchdog.service` | 单元定义：`User=root`、`Restart=always`、`PartOf=`/`BindsTo=` 为空（不随被救的人一起停） |
| `docs/companion-watchdog.md` | 判定表、演练证据、踩坑记录 |

判据来源（以源码为准，别背文档）：`ops/companion-watchdog.py` 的 `DEFAULTS` 里是
`unit` / `health_url` / `state_root` / `window_seconds` / `max_failures`，`evaluate()`
里是实际判定顺序。粗读顺序是：单元不是 `active/activating` → failed；`/alive` 非 200 且窗口
已过 → failed；窗口内 journal 有 Traceback/CRITICAL → failed；否则 healthy。
**认知活动不参与判定**，只记在 `reason` 里。

快照范围由同一文件的 `SNAPSHOT_SKIP` 决定：排除 `data`、`toolchain`、`vendor`、`.venv*`、
`git`、`napcat*`、`docs/acceptance` 等，并跳过 `config.toml`、`*.log`、`*.pyc`。
**它从不回滚数据**——`life.db`、`config.toml`、人格文件都不在快照里。

**它当前是否启用，必须现查，不能假设**：

```bash
systemctl is-enabled companion-watchdog.service   # enabled / disabled / not-found
systemctl is-active companion-watchdog.service
journalctl -u companion-watchdog.service -n 20 --no-pager
cat /var/lib/companion/xiaoyu/data/watchdog/state.json | head -40
```

如果 `is-enabled` 不是 `enabled`，那么「改坏了会自动回滚」这句话就是错的；
正确的说法是「没有人替我兜底，改坏了要人手动回滚」。

---

## 3. 改动怎样变成实际发布

### 3.1 正常路径

1. **在本仓库改**，跑测试（§4）。
2. **同步到云端** `/opt/qq-deepseek`（覆盖同名文件，保留云端独有的数据与 toolchain）。
3. **重启 `companion.service`**——这一步才是「发布」。不重启 = 没发布。
4. **观察**：单元状态、`/alive`、journal 里的启动段、认知轮能不能跑。
5. 若是 watchdog 值守期间：观察窗口内健康则本次源码被标为「已验证」；不健康则回滚到上一份
   已验证快照并重启（§2.4）。

```bash
# 3) 发布
sudo systemctl restart companion.service

# 4) 观察（按顺序看，不要只看第一条）
systemctl show companion.service -p ActiveState -p SubState -p ExecMainStartTimestamp -p MainPID
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8765/alive
journalctl -u companion.service --since '2 min ago' --no-pager | tail -60
```

`/alive` 只证明进程在服务，不证明业务可用。业务健康要看 `journalctl` 里有没有 Traceback
和「启动失败」，以及认知轮有没有真的跑完（`journalctl -u companion.service | grep -i cognition`
或看 `life.db` 的活动记录）。

### 3.2 有没有「不重启就生效」的路径

**基本没有，不要假设有。** 服务是长驻 Python 进程，已 import 的模块不会因为文件改动而重载；
数据类改动（`config.toml`、人格文件、`life-dynamics.json`）有各自的加载/核对路径，
但**代码改动一律要重启**。唯一不需要重启的东西是那些每次调用都现读的**数据文件**
（例如 `skills/*.md` 这类做法库由执行器在调用时读取），它们不是发布内容，是她的资料。

### 3.3 发布后怎样算「这一版真的在跑」

三条同时成立：

1. `ExecMainStartTimestamp` 晚于你改文件的时间；
2. 改动文件的云端 sha256 与你要发布的内容一致；
3. journal 自那次启动起没有 Traceback、没有「启动失败」。

只做 1 和 2 不够：文件对了但进程起来又崩，看起来也符合前两条。

### 3.4 安装与首次部署（不是每次发布都做）

`tools/deploy-companion-install.sh` 是**一次性安装器**：建 `companion` 用户与组、
建 `.venv-companion`、装 `requirements-companion.txt`、把单元文件装到
`/etc/systemd/system/`、`daemon-reload`，**装完不启动**。它拒绝在服务运行时装，
也拒绝在没有 `/opt/qq-deepseek/companion.py` 或 `/var/lib/companion/xiaoyu/config.toml`
时动手。日常发布不走它。

### 3.5 手机端 APK

```bash
# 云端一键：构建 + 发布到下载目录 + 打印 sha256 与签名证书
bash /opt/qq-deepseek/tools/cloud-build-publish.sh
```

- 真正干活的构建脚本是 `tools/build-companion-android.py`，Windows 与 Linux 都能跑；
  工具链优先取 `data/android-toolchain`，缺了退回 PATH。
- 发布的下载目录默认 `/var/www/companion/downloads`（`DEST` 可覆盖），公网前缀默认
  `PUBLIC_BASE`。
- 签名密钥在云端 `/root/.android/xiaoyu-signing`（root 独占），所以发布要 root。
- 判断「这是不是同一个 App」看**包名 + 版本 + 证书**，不看文件哈希：两次构建字节可以不同。
- 细节：`docs/companion-cloud-android-build.md`。

---

## 4. 构建与测试

### 4.1 入口

| 命令 | 用途 | 何时用 |
|---|---|---|
| `` python -B -X utf8 tests/run_isolated_module.py tests.test_xxx `` | 在隔离沙箱里只跑一个测试模块 | 改动局部、要快速反馈 |
| `` python -B -X utf8 tests/run_isolated_suite.py --timeout 900 `` | 整树副本 + 全量 `unittest discover` + `drivetest.py` | 发布前 |
| `` python -B -X utf8 tests/run_isolated_suite.py --self-check-only `` | 只验证隔离本身，不跑用例 | 怀疑沙箱失效时 |
| `` python -B -X utf8 tests/run_isolated_suite.py --db copy --db-source <只读副本> `` | 拿生产库副本跑 | 要复现真实数据问题时 |
| `` python -B -X utf8 tests/run_offline_suite.py `` | 旧的一体化离线套件 | 仅用于复现「外层超时误报」，不作门禁 |
| `` python -B -X utf8 -m unittest discover -s tests -q `` | 原地跑（**不是验收命令**，会碰本机环境） | 仅作耗时对照 |
| `` python -B -X utf8 drivetest.py `` | 驱动力/结算的独立检查 | 改驱动力、记忆结算时 |

### 4.2 隔离机制（`tests/companion_test_isolation.py`）

沙箱保证：临时 `HOME`/`TMPDIR`；`life.db` 是新建空库或用 SQLite backup API 复制的只读副本
（复制前后对源文件取 sha256 断言没变）；源码是**副本**，活树只读；凭据环境变量被删除、
网络与子进程守卫生效；`config.toml` 被中和；生产路径（`/var/lib/companion`、
`/opt/qq-deepseek/life` 等）内的沙箱创建直接拒绝。

`run_isolated_module.py` 的额外约束：`life/__init__.py` 在沙箱里被换成空的命名空间标记，
否则 `import life.xxx` 会把整个包拖进来；`ops/`、`tools/` 下的脚本也一并 staged
（因为有些模块用 importlib 按计算路径加载，AST 扫不到）。

### 4.3 已知坑

- `tests/run_offline_suite.py` 自带硬超时，整棵树全量 `unittest` 在这台机器上比它慢。
  拿它当门禁会得到**假失败**。用 `run_isolated_suite.py --timeout 900`。
- 云端 `/opt/qq-deepseek` 没有 git，测试一律在副本（`/tmp`）里跑；生产 venv 是
  `.venv-companion` 里的 python，加 `--no-deps`。
- 有依赖不在标准环境里，本机用仓库里的 `.test-deps`（脚本默认加进 `PYTHONPATH`）。
- 跑完看 `TREE_CLEAN=true` 和 `home_deleted=true`：前者说明活树没被写脏，后者说明沙箱清干净了。
- 细节：`docs/companion-test-isolation.md`。

---

## 5. 关键约束

### 5.1 沙箱只管子 agent，不管本体

`life/companion_agents.py` 里的 `_agent_paths()` 是子 agent 读写白名单的**唯一裁定处**：
读 = 工作区根 + artifacts + DSH 运行时 + `/usr` `/lib` `/lib64` `/bin`
+ `/opt/qq-deepseek/toolchain` + `/etc/ssl`；写 = 工作区根 + artifacts。
`/var/lib/companion`、`/root`、`/etc/companion`、状态目录永远是禁区。

沙箱模式由白名单推出来，不写死：默认 `workspace-write`，只有配置里显式
`agent_full_access = true` 才是 `danger-full-access`。补丁通过 profile 补丁层覆盖
（同 id 直接给 config），**不能写成 `- insert:`**——那是再挂一个实例，旧的危险模式仍然生效。

**本体（`companion.py` / `Companion`）不在这个沙箱里**：它以 `companion` 用户直接跑在
`/opt/qq-deepseek`，能读写整棵源码树。所以「沙箱挡住越界」这句话只对子 agent 成立；
本体的越界交付物（例如它自己改源码）只能靠 §3 的发布纪律和 watchdog 兜。

### 5.2 `toolchain/`、`vendor/` 只读

- `/opt/qq-deepseek/toolchain` 是子 agent 的**只读**运行时根（DSH 等），也是 watchdog
  快照的排除项。改它等于改运行时，不随项目发布走。
- `vendor/`（含 `vendor/dsh-bridge-work`）是 vendored DSH 源码，watchdog 快照排除、
  测试副本排除。
- `docs/cloud-sync-policy.md` 的热修条款：任何绕过正常同步链路直接落到云端的改动
  （改一行 vendor JS、改 profile、手工替换一个 `.py`）会让此前登记的「云端文件清单 + 哈希」
  立刻过期。规矩是先标 `SUPERSEDED`，改完**必须重跑清单构建**，清单只能整份替换不能追加。
  判定是否过期：清单文件的 `created_at` 早于目标文件的 `mtime`。

### 5.3 `config.toml` 与数据不同步

- 真实 `config.toml`、凭据、人格、`data/`、`life.db`、模型、虚拟环境都**不进 Git**
  （`.gitignore` 已排除），也**不在发布内容里**。
- watchdog 快照跳过 `config.toml`，所以回滚不会把配置一起回滚。
- Git 里只有 `config.example.toml`（模板）。克隆仓库 ≠ 复制运行环境。
- 测试沙箱会中和 `config.toml`，所以「测试里配置是对的」不能证明线上配置对。

### 5.4 其它会咬人的约束

- **`/alive` 免鉴权豁免的位置**：必须在 `CompanionServer.auth` 中间件里；写到
  `MobileAPI.authenticate` 里无效（见 §2.3 与 `docs/companion-watchdog.md` 的踩坑记录）。
- **健康判定不能掺业务指标**：把「有没有说话」「有没有认知轮」当健康标准会造成误判回滚。
- **改含中文的 Python 源文件**不要用 PowerShell 的 `Get-Content`/`Set-Content` 往返，
  会按 GBK 解坏 UTF-8；用 Python 自己做读写，或一次性写完整文件。
- **演练「改坏了」**必须改一个**启动时会被 import** 的模块，改一个没人 import 的新文件
  证明不了任何事。
- **`life/__init__.py` 会重导出整个包**：任何「只在沙箱里 staged 需要的模块」的尝试都要
  先把它换成空命名空间标记，否则隔离不成立。

---

## 6. 相关文档

| 文档 | 读它当你要…… |
|---|---|
| `docs/companion-watchdog.md` | 改自愈逻辑、或要解释「改坏了怎么办」 |
| `docs/companion-test-isolation.md` | 写新测试、判断某次测试失败是真是假 |
| `docs/cloud-sync-policy.md` | 做热修、维护云端文件清单 |
| `docs/companion-cloud-android-build.md` | 出手机端 APK |
| `README.md` | 使用者视角的功能与限制（含本机 Windows 启动方式） |
