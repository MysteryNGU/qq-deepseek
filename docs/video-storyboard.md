# 视频分镜插件接入

使用 [dsh-video-storyboard](https://github.com/SAOS-3624/dsh-video-storyboard) 原生 bundle，固定源码提交 `ebd976d9fd8a7ec9ea6c624437d1926b3047b49e`（0.4.3）。通过 DSH CLI 安装到 Bot 专用 `sdk` profile，保留原 profile 的配置和插件。不自动随 Bot 启动联网安装，不占用桌面 DSH profile。

## 在真机安装

先停止 Bot，再在项目根目录运行。已有 DSH 依赖和编译产物的机器无需重建；缺少时按 [DSH 构建说明](dsh-build.md) 完成。配置 `dsh_root`、`dsh_home`、`dsh_node` 必须指向本机实际部署。

ffmpeg 和 ffprobe 都需要可执行：可放在 **Bot 专用 dsh_home** 的 `tools/ffmpeg/bin/` 下，或设 `DSH_FFMPEG_PATH` 和 `DSH_FFPROBE_PATH`，或加入 Bot 进程的 PATH。显式配置错误不会偷偷换用另一个程序。URL 功能另需插件支持的 yt-dlp；本地 QQ 视频不依赖它。

```powershell
.\.venv\Scripts\python.exe -X utf8 -m life.video_storyboard check
.\.venv\Scripts\python.exe -X utf8 -m life.video_storyboard install
.\.venv\Scripts\python.exe -X utf8 -m life.video_storyboard smoke
```

每一步必须退出码为 0 再继续。自定义配置可在命令后加 `--config 路径`。`check` 仅检查环境；`install` 使用 DSH 的 `plugin --profile sdk add`，核对 manifest 后启动桥接检查四个工具实际注册。安装过程保留 DSH 的版本兼容检查，不自动允许不兼容版本。`verify` 可独立复查注册状态。

`smoke` 生成 4 秒合成视频和合成字幕，实际调用 probe、storyboard、zoom、locate，要求图片工具返回 DSH 图片附件、字幕定位返回正确时间。关闭 ASR，不发送 QQ 消息、不调用聊天模型；临时原视频和字幕自动清理，插件缓存/附件仍按 DSH 生命周期管理。完成后再正常启动 Bot。不要同时运行第二个指向相同 home 的 SDK worker。

## Bot 如何使用

`skills/video-storyboard.md` 是按需读取的方法，通过现有方法目录发现；四个工具沿用真实能力目录、模式锁和权限检查，未加入群聊公共工具白名单。日常状态不常驻加载它们的完整参数。

QQ 视频：`life_read(kind:media,asset_id:...)` 完成现有的范围校验与下载，返回 `asset.storage_ref`；随后将这个实际本地路径交给视频工具。没有额外复制视频到外部服务的 Python 分析链。视频链接可直接作为插件 input；外网下载、登录和 ASR 均遵守原插件配置与当前授权。

优先探查、单张概览、文本定位，再按需放大。抽样帧和转写是证据，推断需要单独表述；没有图片附件不能声称看过画面。完成后恢复日常状态，主人固定工具模式时遵守模式锁。

插件自己的 ASR 配置不会从 Bot 语音识别配置自动复制。需要 ASR 时在 Bot 专用 DSH 环境按上游说明配置；测试命令没有验证外部 ASR、URL 登录或聊天模型的视觉能力。一次拼图的总 token 不固定，取决于模型、图像、清单文字和额外调用。

## 当前验证边界

本次开发环境缺少已构建的 DSH 和 ffmpeg/ffprobe，未完成真实 SDK 注册/视频视觉链路验收。接入相关 37 项离线测试通过，覆盖安装失败、未挂载、缺少工具、假成功无图片、桥接与能力目录；上游本地测试 300 项通过、34 项因缺少外部依赖跳过。真机以 `install`、`smoke` 的实际结果为准，不能把这些离线测试当成已上线。
