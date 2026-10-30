# 三人物迁移到另一台 Windows 电脑

更新：2026-10-31。当前人物为 `xiaoyu`（原始小鱼，界面可改名）、`kafka`（卡夫卡）、`firefly`（流萤）。使用 Git 程序和单独提供的私人 ZIP 恢复现有部署，不重新创建人物。

## 两份交付

- 程序、网页、多人物管理、配套定制 DSH 源码、维护脚本和文档：`https://github.com/MysteryNGU/qq-deepseek` 的 `main`。归档 `migration/manifest.json` 记录对应提交。
- 私人 ZIP：三位人物的配置、人格、现有记忆、关系和任务、DSH 会话、固定音色、已生成媒体、管理页登录资料、NapCat 配置、私人脚本与所引用的插件/咨询项目资料。

ZIP 未加密，包含真实密钥和会话，只在自己的可信设备间转移。Git 不包含这些私人资料。已退役隔离的旧小鱼数据库不在本次迁移范围。

QQ/NapCat 程序、Python 虚拟环境、Node 依赖及大型模型权重不属于这份资料包，需要新机安装或单独复制。当前生图和语音模型合计约 19 GB；不能把资料导入成功说成这些能力已经恢复。Codex 主账号的全局登录态不复制，由用户在新机登录。

## 1. 准备程序，暂不启动人物

建议继续使用 `D:/projects/qq-deepseek`，减少媒体资产与工具路径调整。新机若已有仓库或数据，先保留，不使用 reset/clean 或覆盖数据库。

```powershell
git clone https://github.com/MysteryNGU/qq-deepseek.git D:\projects\qq-deepseek
Set-Location D:\projects\qq-deepseek
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

配套 DSH 位于 `vendor/dsh-bridge-work`，按 [DSH 构建说明](dsh-build.md) 安装 Node、锁定的 pnpm 与依赖，并构建 native/host。主项目的网页由 Python 提供，不需要构建 DSH Desktop/Web。

## 2. 解压私人资料

先解压到独立临时目录，阅读 `迁移说明.txt` 和 `migration/manifest.json`，不打印密钥或私人正文。

- `project/` 下内容按相对位置合并进新项目根目录。这里已有 `data/characters/registry.json` 和三个人物的完整部署快照。
- `external/config-assets/` 按清单 `roots` 中的 `setting / source / archive` 映射恢复。包含所引用的 B站插件和专用咨询项目资料，不能误当成公共空白模板。
- `components/index-tts-source/` 是本机现用 IndexTTS 源码，不含其虚拟环境或模型，供重建语音运行环境。
- `recovery-only/` 保存旧发布控制器/管理操作材料，只供核对，不覆盖新机活跃的发布控制数据库，不自动重放旧行政命令。
- SQLite 文件由备份 API 生成，包含已提交的 WAL 内容；不要再拷贝旧 `-wal`、`-shm` 或 `-journal`。

私人包内所有人物均为停止状态。旧电脑同样保持人物停止，避免两个副本同时回复。不要删除 `primary-retired.json` 重新启用旧单人物入口。

## 3. 必须核对的本机路径

分别检查主目录和 `data/characters/{xiaoyu,kafka,firefly}/config.toml`，不只修改主目录：现有人物不会自动继承主目录后续变化。

| 配置 | 新机要求 |
| --- | --- |
| `life_agent`、`life_db`、`persona`、`dsh_home` | 保留各人物编号及独立目录，不共享库或模型历史 |
| `dsh_root` | 指向新机 `vendor/dsh-bridge-work` 的绝对路径；人物根目录不同，不能直接填主目录的相对路径 |
| `dsh_node`、`dsh_pnpm_entry` | 新机实际 Node 和 pnpm 入口 |
| `dsh_tmp` | 各人物自己的临时目录 |
| `bili_plugin` | 恢复到新机实际插件目录；重装该插件 Python/Node 依赖 |
| `local_embedding.model_path` | 新机 BGE 模型目录；保留 manifest digest 和版本 |
| `local_embedding.port` / `embedding.endpoint` | 小鱼 8231、卡夫卡 8232、流萤 8233，端口与 URL 成对一致 |
| `local_media.providers` | 改正新机的程序、Python 和模型路径；媒体任务根目录继续各自独立 |
| `consult_config` | 指向各人物自己的咨询 JSON，检查其中 codex、python、project、data_dir |
| `media_local_roots` | 按新机 QQ 实际缓存位置配置，不沿用不存在的旧缓存目录 |

现有人物带有各自的程序快照。如果只安装了主目录的依赖，仍可用主 `.venv` 的解释器启动人物，但 DSH workspace 与每个 `data/dsh-home/profiles/sdk` 的 Node 包依赖都需要恢复。保留 profile、归属标记和会话 ID，修正 package/link 的机器路径，不删除 profile 重建角色。

配置与 JSON 中的人物素材路径可按已确认的旧根目录到新根目录映射调整。改用其他项目路径时，还要检查数据库的资产引用与任务产物路径；不要对整个 SQLite 二进制或所有文本盲目替换用户名。

## 4. 本地能力

### 向量检索

在主虚拟环境安装 `requirements-embedding.txt`，运行 `scripts/install_local_embedding.py` 安装固定版本 BGE 模型，或复制旧机 `data/models/bge-small-zh-v1.5`（约 96 MB）。三个角色可共用只读模型文件，各自的索引和服务端口独立。保留模型 manifest 校验，不更改摘要来绕过错误。

### 语音与音色

恢复 IndexTTS 源码后，在新机创建其专用 Python 环境并安装兼容的 PyTorch/CUDA 依赖。模型是配置里记录的 IndexTTS-2.5 revision，旧机权重目录见清单 `paths_to_review`（约 7 GB）。

每个人物的 `data/voice-presets` 已包含清单和实际 reference/sample WAV，不要重新生成相似音色替代。保留 SHA256，调整路径后由该人物自己的目录导入。旧机公共合成音色定义也在包内。

### 生图

安装与配置版本匹配的 sd.cpp Windows CUDA 程序，准备 Qwen 权重和辅助模型（约 12 GB）。四个必要文件：`qwen-image-2.1-turbo-Q4_K_M.gguf`、`Qwen3-VL-8B-Instruct-UD-Q4_K_XL.gguf`、`mmproj-F16.gguf`、`vae/qwen_image_2.1_vae_bf16.safetensors`。按新机显卡检查显存限额，不直接把旧机测试结果当成新机已可用。

### 咨询与其他工具

安装并登录 Codex，使用 `(Get-Command codex).Source` 核对当前可执行文件。各人物咨询配置的 `data_dir` 必须各自独立；恢复专用咨询项目的 INIT/AGENTS 等文件，不自动重发旧请求。B站仅先检查已配置插件的只读能力，登录失效时由用户重新授权。

## 5. QQ 和管理页

安装兼容的 QQ/NapCat，导入相应 `config/` 后由用户扫码。校验实际登录账号与每个人物 `bot_qq` 一致。当前只配置并运行过小鱼；卡夫卡和流萤的 QQ 不可臆造。

```powershell
Set-Location D:\projects\qq-deepseek
.\.venv\Scripts\python.exe -m life.settings_web --root .
```

管理页为 `http://127.0.0.1:6100/`，密码在 `data/settings/login.txt`。NapCat 的 6099 管理端口与人物的正向 WebSocket 端口不同。检查好路径、账号与模型后，从管理页逐个启动人物。

## 6. 最小验收

确认三张人物卡、名单和音色仍在，分别检查进程、QQ、认知与本地向量状态。由用户发送一条真实私聊验证回复，再读取一条已有记忆核对身份与来源。需要完整媒体能力时，再验证一条短语音和一张图片，不把配置存在或模型文件存在当作生成成功。

报告 Git 提交、实际资料包、已恢复与待安装的能力。依赖未齐时明确列缺项，不清空记忆或把未安装能力伪装成已启用。
