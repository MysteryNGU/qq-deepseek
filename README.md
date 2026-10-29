# 小鱼 · QQ 持续生活助手

主人交办的工作在工具状态下，连续完成 6 次工作工具调用或已有工具结果且沉默达到 90 秒时，会在下一次工具执行前要求主角色先反馈实际进展或具体阻碍。真实送达后再继续，正常聊天和作业状态查询、停止操作不受此限制。同一操作连续 3 次得到完全相同结果时，阻止更换调用编号原样重试；新用户输入可重新决定尝试。该检查发生在工具边界，不主动发送定时占位消息，也不将历史 `started` 请求当作当前进展。

当前代码版本：`2026.10.27.15`。说明更新于 **2026-10-27**。

小鱼通过 NapCat 接收 QQ 私聊、群聊，由 DSH 主角色理解消息和执行工具；本项目负责人物与场景范围、记忆、驱动力、持续任务、媒体发送和运行管理。所有场景使用一份人格，按真实用户画像、关系和场景调整表达。

工具上下文会直接列出可确认的输入编号，理解后可随下一次发言或休息一并确认。专项覆盖摘要只带当前受众的未解决项和已处理计数，明细通过 `life_read(kind:simulation,view:input_coverage)` 分页读取。历史压缩按完整工具往返处理已解决的输入错误，每个运行中的轮次最多尝试一次；原始记录、未读输入检查和未知操作保护仍保留。

完整模型请求装配后，会展示这次真正已读输入的固定批次。理解整批后可随发言或休息提交 `handled_batch_ref`，只理解部分时仍用逐条编号；后来到达的消息继续保留，停止和发送保护照常执行。工作前应答按具体来源的真实送达核对，内部唤醒不要求重复告知。相互独立的只读检查优先合并执行，正常私聊表达、进度反馈和有先后依赖的操作继续保留。

QQ 图片优先从已配置媒体目录内的平台缓存取回；公共 QQ CDN 使用保留证书校验的直连下载，避免无效系统代理阻断图片。`life_read(kind:media,asset_id)` 重新投递原图，主人本机截图可通过 `kind:media,action:import,path` 导入并查看；两条路径均将实际像素送入同一模型会话，继续校验来源、受众和遗忘状态。

本 README 说明当前实现和使用方式。详细交付范围见 [实施状态报告](docs/life-implementation-status.md)。设计文档中的候选方案和旧阶段记录不等于已经上线。

## 当前功能与限制

| 功能 | 当前情况 |
|---|---|
| 统一设置网页 | 本机 `http://127.0.0.1:6100/`，覆盖 53 个主配置字段及独立动力、咨询和已登记音色配置；复杂参数可编辑 JSON |
| 配置保存与生效 | 字段校验、版本冲突保护、原子保存、密钥遮蔽；保存后等待当前操作结束再重启，核对实际加载版本 |
| 统一人格 | 私聊和群聊使用同一人格；多人格卡管理及切换需求已取消，旧兼容字段不作为当前配置方式 |
| 人物画像 | 根据真实发言人登记、按来源更新画像或记录无新增；支持历史消息分批复核，不能把身份登记当作详细画像已完成 |
| 记忆 | 原始经历即时入库和词法检索；同一人的群聊内容可衔接与本人的私聊；其他材料仍受可见范围限制 |
| 摘要与向量 | 小时整理、向量检索和原文读取分开；不能以定时器已运行推断摘要完成，也不要求等待摘要才能读取原话 |
| 驱动力与活动 | 动机形成候选，行动或明确延期/放弃；普通持续事项有检查点、暂停/恢复、停止及同人群聊转私聊接续 |
| 唤醒 | 一轮全部操作结束后，随机等待 10～60 分钟；真实消息、结果和明确约定按各自触发处理 |
| 图片与语音 | 本地生成、排队执行、真实发送回执；语音支持模型保温，图片/表情收藏有范围、数量和有效期 |
| 进程管理 | PowerShell 看守、Python supervisor、宿主健康检查和设置服务；QQ 在线与宿主在线分别显示 |

**最近验收情况：** 693 项单元回归、42 项驱力检查通过；设置网页登录、保存、错误输入阻止保存及重启生效已实测。按用户要求，以基本可用为准，未扩大长期体验验收。

**最近运行快照：** 新版宿主和设置网页已运行、配置已加载；当时 QQ 未登录，认知服务未就绪。用户选择暂不登录，因此真实聊天、历史画像后台模型效果及长期使用质量仍待实际使用观察。实时状态请看设置网页，不把本段快照当作在线保证。

**取消的工作：** 费用对账与旧整理批次恢复不在本轮交付内，原记录和检查点保留；多人格卡管理已取消。原专项体验机制没有扩展到普通行为。

## 在这台电脑上使用

- **启动完整服务：** 双击 `一键启动.bat`。它通过 `run.ps1` 启动 NapCat 与受监督的 Bot；需要时完成 QQ 扫码登录。
- **设置小鱼：** 打开 <http://127.0.0.1:6100/>。首次本机管理密码在 `data/settings/login.txt`。修改后依次点击“保存修改”“重启并生效”。
- **QQ 登录管理：** <http://127.0.0.1:6099/webui> 是 NapCat 页面，与小鱼设置网页不同。
- **状态与停止：** 使用 `状态.bat`、`停止.bat`。小鱼设置服务独立于 Bot 重启，停止 Bot 不等于设置端口关闭。

已启动 NapCat 时，可单独运行受监督的 Bot：

```powershell
.\.venv\Scripts\python.exe supervise.py
```

只需离线修改配置、无需启动聊天时，可单独启动设置服务；已有实例运行时不必重复执行：

```powershell
.\.venv\Scripts\python.exe -m life.settings_web --root .
```

配置中数据库、角色身份和统一人格来源在网页只读，避免普通设置操作变成数据迁移。环境变量覆盖项会提示；多个配置文件分别保存，部分失败时保留未成功保存的草稿。

## 新机器部署需要什么

已有个人配置包、准备交给另一 Agent 完整迁移时，直接使用 [完整迁移交接说明](docs/migration-agent-handoff.md)，其中包含 Git 获取、隐私包导入、DSH/插件环境、素材补取和最小启动检查。

本地部署副本最完整，Git 保存可维护源码和模板，**克隆仓库不等于复制完整运行环境**。

1. 准备 Windows、Python 3.11+ 和虚拟环境，安装 `requirements.txt`。
2. 复制 `config.example.toml` 为 `config.toml`，填写真实 `owner`、模型接口/凭据、允许的好友和群、NapCat 连接及 DSH 路径。
3. 复制 `persona.example.md` 为 `persona.md`，设置一份统一人格，保持 `unified_persona = true`。
4. 安装 Node 和 NapCat，构建配套 DSH。定制源码已包含在 `vendor/dsh-bridge-work`，新机按 [DSH 构建说明](docs/dsh-build.md) 安装依赖并构建 Host。真实配置中的 `dsh_root` 须指向 `vendor/dsh-bridge-work`；Git 拉取不会修改本机 `config.toml`。
5. 语音、生图、本地向量模型、音色素材及相关运行环境按需配置。向量依赖单列在 `requirements-embedding.txt`，不要把普通聊天接口当作向量接口。
6. 启动并扫码，先确认宿主、QQ、认知服务分别就绪，再观察普通聊天。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

示例配置不是开箱即用凭据，也不包含第三方模型权重。迁移本机时，需要另外备份真实配置、人格、数据及外部运行环境。

## 目录与维护入口

| 路径 | 用途 |
|---|---|
| `bot.py`、`life/role_entry.py`、`life/role_host.py` | QQ 宿主入口与生命周期 |
| `life/role_agent.py`、`life/services/dsh_bridge.py` | 主角色、生活工具、DSH 会话桥 |
| `life/people.py`、`life/profile_backfill.py` | 人物认知与历史复核 |
| `life/activity_driver.py`、`life/tasks.py` | 普通连续活动与任务状态 |
| `life/life_*.py`、`life/memory_*.py` | 经历目录、范围、检索、整理与索引 |
| `life/settings.py`、`life/settings_web.py`、`life/settings_ui/` | 设置校验、服务与网页 |
| `life/local_media.py`、`life/media_*.py` | 媒体生成、处理与收藏 |
| `supervise.py`、`lifesupervisor.py`、`tools/bot-watchdog.ps1` | 监督启动与恢复 |
| `tests/`、`drivetest.py` | 自动回归；`tests/manual/` 为按需人工验收，不等于全部已执行 |
| `docs/` | 当前状态、需求、概要/详细设计与维护说明 |
| `vendor/dsh-bridge-work/` | 当前配套定制 DSH 完整 workspace 源码；新机安装依赖并构建 |

- [当前实施状态](docs/life-implementation-status.md)
- [需求基线](docs/life-continuity-requirements.md) · [历史需求参考](docs/historical-requirements.md)
- [概要设计](docs/life-overview-design.md) · [详细设计](docs/life-detailed-design.md)
- [记忆维护说明](docs/memory-operations.md) · [语音延迟说明](docs/speech-latency.md)
- [记忆与主动陪伴增强设计](docs/companion-enhancements.md)：事实提取/修订、按问题召回与诊断、自发联系节奏及连续道别处理。
- [视频分镜插件接入](docs/video-storyboard.md)：原生工具按需加载、QQ 视频解析、固定版本安装和真机视觉验收。

需要时执行离线回归：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -q
.\.venv\Scripts\python.exe -X utf8 drivetest.py
```

## Git 与本地资料

迁移到另一台电脑时，双击 `导出个人配置.bat`。默认在 `backups/` 生成仅含真实配置（含当前人格和凭据）、记忆及 skills 的迁移 ZIP，默认只打包、不做额外校验。需要时可显式添加 `--verify`。脚本也可直接运行：

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts/export_private_bundle.py
```

认知服务在线时请先停止 Bot。压缩包未加密，含真实密钥，只在自己的设备间转移，不上传 Git。包内 `迁移说明.txt` 和 `migration/manifest.json` 列出目录映射、排除项和需要调整的路径。SQLite 使用一致性备份；不包含源码、旧发布控制库或维护命令。音色音频/图片素材、模型权重、虚拟环境、node_modules、第三方安装包及 QQ 登录缓存不包含，需另行复制/安装或重新登录。迁移后不要让两台机器同时运行同一个 Bot。

Git 保留当前源码、启动工具、空白模板、必要维护脚本、回归测试和仍有效的需求设计。真实 `config.toml`、凭据、人格、`data/`、记忆、聊天、媒体、模型、虚拟环境及本机学习资料不提交。

2026-10-27 已从版本库移出失效的旧入口/调试脚本、旧公开人格模板、一次性探针及旧验收快照；本地原文件仍保留，旧 README 与清理清单保存在 `data/maintenance/git-cleanup-20261027/`。运行模块和仍有引用的设计没有因“看起来旧”而删掉。整理针对当前文件树，不重写 Git 历史。
