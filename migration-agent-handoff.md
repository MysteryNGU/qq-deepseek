# 小鱼完整迁移：交给新电脑 Agent 的执行说明

更新：2026-10-27。目标：在另一台 Windows 电脑恢复现有 Bot 的配置、记忆、skills 和已配置能力。不是重新设计 Bot，也不是创建一个空白的新角色。

## 直接给 Agent 的任务

> 请按本文完成小鱼迁移。主项目源码从 Git 获取，用户另行提供的精简 ZIP 只用于导入配置、记忆和 skills。先阅读 README 和当前实施状态，检查缺失依赖，再安装环境、导入资料并调整路径。普通可逆部署工作直接继续；需要扫码/账号登录时交给用户。缺失的本地定制源码或私人素材请一次性列明，不能用原版或新生成内容悄悄替代。以基本可用为验收尺度，不跑大规模回归、长时间压力测试或额外压缩包校验。完成后报告实际能用的能力及仍缺的材料。

## 1. 先认清三个来源

| 来源 | 提供什么 | 不提供什么 |
|---|---|---|
| `https://github.com/MysteryNGU/qq-deepseek.git`，`main` | Bot 当前源码、网页、启动工具、依赖声明、通用维护脚本和设计 | 真实凭据、人格、记忆、模型、外部定制 DSH 工程 |
| 用户提供的精简 ZIP | 当前配置和人格、life.db、日记/self、skills、专用 DSH 配置/会话/持久存储、NapCat 配置和设置页登录资料 | 源码、插件代码、scripts、媒体素材、权重、虚拟环境、node_modules、第三方安装包 |
| 官方安装源或旧电脑补取 | Python/Node/QQ/NapCat、模型运行环境、定制 DSH、插件和个人素材等 | 不保证公开上游包含旧机的本地补丁 |

本次精简包名：`xiaoyu-private-migration-20261027-094202.zip`，178 个文件，约 91 MiB。用户会另行发送，实际位置由用户提供。不要使用此前误打包的约 578 MB 大包作为必需输入。

**仅有 Git + 精简 ZIP，不能保证恢复全部功能。** 下列定制 DSH、插件和私人素材可能需要另行获取。不能在缺失它们时声称“全体获取完成”。默认不做 ZIP 哈希/逐文件校验，正常解压失败则如实处理。

## 2. 不要改变的既有需求

- 保留一份统一人格，按真实用户画像、关系和场景表达，不恢复多人格卡切换。
- 保留既有 life_agent、人物身份、记忆、关系、任务和 DSH 专用会话；不要清空数据库或重建成另一个角色来绕过错误。
- 空闲唤醒为上一整轮操作结束后随机 10～60 分钟。普通持续任务的续行另外处理。
- 费用对账与旧整理暂停/隔离批次恢复已取消本轮处理；迁移不构成恢复、清零或重新提交这些任务的授权。
- 旧操作 unknown、等待用户答复等状态不得改成成功或重新执行以“消除错误”。
- 用户已要求基本可用即可；不用跑全部 693 项回归、GPU 基准或七天观察，也不用为迁移重构代码。
- 私有 ZIP 含明文密钥。配置、数据库、会话、Cookie 和人格只在用户设备之间转移，不提交 Git，不打印到报告。

## 3. 先拿到代码，暂不启动 Bot

推荐目录（新机可调整）：

```text
D:\projects\qq-deepseek       主项目
D:\projects\dsh-bridge-work   此 Bot 配套的定制 DSH 源码
D:\projects\dsh-qq-home       此 Bot 的独立 DSH 数据目录
D:\projects\bot-codex         可选 Codex 咨询工作目录
```

先安装 Git，在空目标目录克隆：

```powershell
New-Item -ItemType Directory -Path 'D:\projects' -Force | Out-Null
git clone https://github.com/MysteryNGU/qq-deepseek.git D:\projects\qq-deepseek
Set-Location D:\projects\qq-deepseek
git status --short
git log -1 --oneline
```

如果已有同名项目，先检查其修改和数据，不能直接删除、`reset --hard` 或 `clean`。干净分支可 `git pull --ff-only`。使用当前 main；`1691787` 是精简导出修正提交，可作为历史定位，不必固定回退到它。

阅读 `README.md`、`docs/life-implementation-status.md`、`config.example.toml`。这次有真实配置 ZIP，**不要拿 example 覆盖导入的真实配置或人格**。

## 4. 解压并导入精简 ZIP

旧电脑 Bot 应保持停止，新电脑暂不运行 Bot，避免两个实例同时回复或写入各自记忆。ZIP 是导出时的快照，导出后旧机新增内容不会自动同步。

解压到独立临时目录，而不是直接覆盖运行中的项目：

```powershell
# 把此处改成用户实际提供的 ZIP 路径。
$PrivateZip = 'D:\transfer\xiaoyu-private-migration-20261027-094202.zip'
$Stage = Join-Path 'D:\transfer' ('xiaoyu-import-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
Expand-Archive -LiteralPath $PrivateZip -DestinationPath $Stage
```

只阅读包内 `迁移说明.txt` 和 `migration/manifest.json` 的结构、路径映射及缺项，不把私人正文和凭据回显给用户。

- `project/` 下的内容按相对位置并入主项目根目录：例如 `project/config.toml` → `qq-deepseek/config.toml`，而不是再建立 `qq-deepseek/project/`。
- `external/dsh-home/` 下的内容放入新机专用 `dsh-qq-home/`，不是整个电脑共用的 DSH home。
- `migration/environment.private.json` 仅记录导出进程可见的特定环境覆盖。检查是否需要在新机设置；不要执行其中的内容或将它直接公开。
- 首次导入仅在空白/刚克隆的部署中合并。若新机已有配置和数据库，先完整备份这些目标，不能混合两个 life.db 或让旧 WAL 覆盖导入库。

新目录导入示例：

```powershell
$Repo = 'D:\projects\qq-deepseek'
$DshHome = 'D:\projects\dsh-qq-home'
Get-ChildItem -LiteralPath (Join-Path $Stage 'project') -Force | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $Repo -Recurse -Force
}
New-Item -ItemType Directory -Path $DshHome -Force | Out-Null
Get-ChildItem -LiteralPath (Join-Path $Stage 'external\dsh-home') -Force | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $DshHome -Recurse -Force
}
```

导出的 life.db 已通过 SQLite 备份 API 合入当时已提交的 WAL 数据，不需要从旧机再拼接 `.db-wal`/`.db-shm`。本包不包含模型、音色音频及历史图片；数据库保存了资产引用不代表实际文件已带来。

## 5. 环境和外部材料清单

### 5.1 Python 与主项目

使用 Windows Python 3.11 或以上、兼容依赖的版本，在新机创建虚拟环境，不直接搬旧机 `.venv`：

```powershell
Set-Location D:\projects\qq-deepseek
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

保留 requirements 的版本范围。若 GPU 包需要不同解释器，为媒体引擎单独建立环境，不随意升级主项目全部依赖。

### 5.2 定制 DSH：核心必需，不能用任意上游版替代

旧机实际源码位置：`D:\projects\dsh-bridge-work`。本机检查时该目录不是 Git 工作树；它不在 qq-deepseek Git，也不在精简 ZIP。当前项目依赖其中的 QQ life、记忆整理、连续活动、媒体、咨询等定制协议。

**优先获取旧机这份工程，或包含相同补丁的用户指定仓库/归档。** 目前没有确认一个可直接还原这些本地补丁的公开 Git 地址；缺文件时向用户索取，不猜仓库、不拿原版安装成功冒充兼容。

至少需要完整工程的 `package.json`、锁文件、workspace 配置、`apps/`、`packages/`、`scripts/`、`native/`、`vendor/` 和必要资源。关键实现包括 `packages/sdk/server/src/qq-life-sessions.ts` 及其他配套 SDK 文件；只复制这一份文件不能替代整个匹配版本。

旧机 package.json 的已知基线（不是最新版本推荐）：

- 包版本 `0.2.1-rc.2`。
- Node 范围 `^22.19.0 || >=24.0.0`，旧机采用 Node 22.19.0。
- 包管理器 `pnpm@11.28.5`；以实际收到工程的 packageManager 和 lockfile 为准。
- 桥接实际以 `node --import tsx/esm apps/cli/src/bin.ts --profile sdk` 启动。

在收到的工程中按其说明安装固定依赖，优先锁文件安装，不忽略安装失败。只构建 CLI/SDK/宿主需要的产物；原工程有 native Windows 依赖，必要时按报错补编译工具。不要把完整 Desktop/Web 构建当作本次迁移必须门槛，也不修改桥接协议来绕过未安装的组件。

恢复 `dsh-qq-home/profiles/sdk` 的包依赖和本地 link 路径。保留 profile 归属标记及会话身份；缺少 node_modules 应重新安装，不删除 profile 重新创建另一个角色。

### 5.3 QQ 与 NapCat

安装兼容当前协议配置的 Windows QQ 和 NapCat。旧机使用 `napcat-onekey/NapCat.52230.Shell/`；`run.ps1` 默认寻找这个路径，改用其他目录时需调整实际启动配置。

从 ZIP 恢复对应 `config/`。重新登录由用户扫码，不能假定 Cookie 或登录缓存跨机有效。确认：

- OneBot WebSocket 与 `config.toml` 的 `ws_url`/`ws_token` 一致；旧机为 `127.0.0.1:3001`。
- NapCat 登录页旧机端口为 6099，实际以安装结果为准。
- 小鱼设置网页是 6100，和 NapCat 页面不是同一服务。

不要为迁移关闭身份校验、公开监听管理端口或清空原好友/群配置。

### 5.4 B站插件：代码与账号配置分别恢复

旧机配置引用 `C:\Users\GEUEK\.dsh\plugins\dsh-bili-subagent`。其 package.json 记录版本 `0.8.3`，上游来源为 `https://github.com/Yuuyuko-uu/dsh-bili-subagent`。优先带旧机版本；从上游获取时核对对应版本和可能的本地修改。

旧机专用 DSH home 还存在 `plugins/dsh-bili-whale-main`（包名 `dsh-bili-whale`，版本 `0.4.5`）及本地 tar.gz。这个插件没有在本次核对中确认公开仓库，**代码不在精简包**。先读导入的 sdk profile，确定实际引用哪些插件；引用的每一个都须准备代码，而不是只安装 subagent 就认为完整。

把插件安装到 Bot 的 `sdk` profile，按插件实际声明恢复包/link 和所需 Python/Node依赖。不要照抄插件 README 里的 `--profile desktop` 到本 Bot。登录/Cookie 使用导入的专用配置；失效时请用户重新授权。

本次只需确认插件加载和只读能力可用，不自动发表B站评论、动态或私信。

### 5.5 本地向量模型

当前模型：`BAAI/bge-small-zh-v1.5`，旧机 revision `7999e1d3359715c523056ef9478215996d62a620`；CPU 服务通常为 `127.0.0.1:8231`。

模型权重不在 ZIP。可复制旧机 `data/models/bge-small-zh-v1.5/`，或使用仓库内的固定版本安装器：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-embedding.txt
.\.venv\Scripts\python.exe scripts\install_local_embedding.py
```

安装器自带模型完整性要求属于正常安装流程；用户“不额外校验”不是要求绕过加载保护。保留正确的模型 revision、维度、provider revision、manifest_digest，不能改字段假装匹配。索引是派生数据，原始记忆保留；先让词法/原文读取可用，不等待全部历史向量完成再启动普通聊天。

### 5.6 生图、语音和私人音色素材

读取真实 `local_media.providers`，按新机 GPU/驱动配置对应环境，不照搬旧显存数值。

| 能力 | 旧机实现与关键位置 | 新机需要 |
|---|---|---|
| 生图 | `life.media_adapters.qwen_sdcpp`；Qwen-Image-2.1-Turbo-Q4_K_M；`data/media-feasibility/sd-cpp/sd-server.exe` | 匹配的 sd.cpp 程序及依赖、Qwen 权重与辅助模型；按实际 provider 配置定位 |
| 语音 | `life.media_adapters.indextts`；IndexTTS-2.5；`data/media-feasibility/index-tts/` | 对应引擎源码、独立 Python/PyTorch/CUDA 环境和 checkpoint |
| 音色 | `data/media-feasibility/voice-presets/manifest.json` | 清单已在 ZIP；reference/sample 的 WAV 文件必须从旧机补取 |
| 历史资产 | `data/media/`、`data/local-media/`、`data/voice-refs/`、`data/stickers/` 等 | 若要求旧图片、语音、收藏仍能打开，按数据库引用补取实际文件 |

旧机两个权重根目录分别在用户目录 `.cache/qq-deepseek-media-feasibility/qwen` 和 `tts-checkpoints`；新用户名不同必须修改路径。这些不是小鱼 Git 中的内容。

不要重新合成“差不多”的音色来替代缺失 WAV，不更改已有素材摘要来绕过校验。恢复媒体后还需核对 voice catalog、冻结任务和文件路径是否指向有效资源；必要时按现有登记接口迁移，不盲目改 SQLite 文本。

### 5.7 Codex 咨询（已配置能力）

主项目桥接代码和通用 skill 已在 Git，真实咨询配置在 ZIP 的 `project/data/codex-consult/config.json`。

新机安装并登录用户选择的 Codex 客户端/CLI，按当前可用安装位置设置 `codex` 可执行路径；不要直接使用旧用户名和旧版本目录。核对 `project`、`data_dir`、`python`。旧机专用咨询项目 `D:\projects\bot-codex` 的 INIT/说明文件不在精简包，要求完全恢复时另行补取。

精简包不包含 Codex 主账号的登录态，也不包含项目之外的 Codex 会话数据库。不读取/复制无关账号凭据；需要登录时让用户完成。先按桥接的帮助/状态入口做轻量检查，不自动恢复或重发旧咨询任务。

### 5.8 手机工具与用户自学脚本

Git 有 `phone.py`、`qqgui.py`，但 adb、ADBKeyboard、模拟器和手机登录环境另行安装。旧机 `run.ps1` 使用 `tools/platform-tools/adb.exe`，是否需要模拟器由用户实际使用情况决定。

精简包包含 skills 文本，**不包含用户要求排除的 scripts 代码和其他私有程序**。Agent 应阅读 skill 中引用的本地文件名：若引用 Git 没有的脚本（如本机生成的脚本或 `qzone_post.py`），把这些依赖列入补取清单。拿到一份 skill 不代表它依赖的工具已经安装。

## 6. 路径调整顺序

1. 修改真实 `config.toml` 中 `dsh_root`、`dsh_home`、`dsh_node`、`dsh_pnpm_entry`、`dsh_tmp`、`bili_plugin`。
2. 核对 `persona`、`life_db`、`consult_config`，保留角色身份及实际数据，不换成模板。
3. 修改媒体 provider 的 `environment.python`、`image_server`、`image_model_dir`、`voice_engine_root`、`voice_model_dir`，以及 `local_embedding.model_path`。
4. 修改音色 manifest 的 reference/sample 路径，保留实际音频内容与摘要；核对旧资产路径。
5. 核对 DSH sdk 的 package/link、patch、profile 归属路径和专用 home；只改机器相关路径，不清除会话 ID。
6. 核对咨询配置、INIT 文档及 skills 引用路径。最后处理环境变量覆盖，防止导入配置被旧变量盖掉。

优先沿用 `D:\projects` 可以减少迁移量，但不是无需核对。不要对全部目录/二进制数据库执行批量字符串替换。

## 7. 启动与基本可用检查

配置和材料未齐时不要启动有外部动作的 Bot。可先单独打开设置服务：

```powershell
Set-Location D:\projects\qq-deepseek
.\.venv\Scripts\python.exe -m life.settings_web --root .
```

页面 <http://127.0.0.1:6100/>，本机密码在 `data/settings/login.txt`。界面只读字段通过受控离线配置调整，不解除网页限制。

准备完成后双击 `一键启动.bat`；若 QQ 已运行，可以用 `.\.venv\Scripts\python.exe supervise.py` 单独启动宿主。旧机保持停止。

仅做必要的基本检查：

- 网页可登录，实际配置已加载；宿主、QQ 和认知状态分别看，不能以网页打开推断 QQ 可用。
- 让用户发一条普通私聊，小鱼能接收并回复；不伪造真人入站消息。
- 读取一条既有普通记忆，确认是导入的记录；群聊仅在用户确实需要时做一次普通衔接。
- 需要完整媒体能力时各做一次短语音和简单生图，确认真实文件生成及可发送；不跑基准批次。
- B站/咨询/手机工具只检查实际已恢复的入口；不因缺少可选功能而宣称整体全部完成。

最后给用户一张简短的结果表：已恢复、缺材料、需登录、尚未启用。注明 Git 提交、实际目录、配置页入口和待用户提供的文件，不附任何密钥。

## 8. 缺材料时一次性向用户索取的清单

先查用户已提供的位置或共享盘，确实没有再集中询问：

1. **核心必需：** 旧机定制 `dsh-bridge-work` 工程或等价补丁仓库。
2. **按 profile 的实际引用：** `dsh-bili-subagent`、`dsh-bili-whale-main` 插件代码及本地修改。
3. **保持现有音色必需：** voice-presets 中 manifest 引用的 reference/sample WAV；要求历史媒体可用时追加数据库引用的图片、音频和收藏目录。
4. **完整媒体能力：** IndexTTS 源码/版本和权重、sd.cpp 程序、Qwen 及辅助模型；可公开下载的部分自行安装，私人素材必须用户提供。
5. **完整私人方法/咨询：** skills 引用但 Git 没有的本机脚本，以及 `bot-codex` 专用初始化资料。
6. **交互授权：** QQ 扫码、失效的B站登录、Codex 登录等由用户在新机完成。

不要重新向用户索取已在精简 ZIP 中的 API key、人格或 life.db。不要把“公开依赖可以重新下载”和“私有补丁/素材已经取得”混为一谈。
