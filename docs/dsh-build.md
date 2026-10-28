# 配套 DSH：Git 获取、构建与迁移

更新：2026-10-29。此说明面向另一台 Windows 电脑，和 [完整迁移交接说明](migration-agent-handoff.md) 一起使用。

## 1. 一次克隆获取 Bot 和定制 DSH

```powershell
git clone https://github.com/MysteryNGU/qq-deepseek.git D:\projects\qq-deepseek
Set-Location D:\projects\qq-deepseek
```

已有干净仓库则执行 `git pull --ff-only`，有本地修改时先保留并合并。配套源码位于 `vendor/dsh-bridge-work`，不需要另找 DSH 压缩包或初始化子模块。此快照包含本地定制 SDK 协议，不用原版 DSH 替换。

## 2. 新电脑需要安装依赖并构建

Git 提供源码和锁文件；`node_modules`、`lib`、原生依赖和构建缓存由新电脑生成。旧机器的虚拟环境和 Node 依赖不能直接当成新电脑的安装结果。

该快照声明 Node `^22.19.0 || >=24.0.0`，旧部署使用 Node 22.19.0；包管理器固定为 `pnpm@11.28.5`。先安装满足该范围的 Node，再在 PowerShell 执行：

```powershell
node --version
npm install --global pnpm@11.28.5
pnpm --version
Set-Location D:\projects\qq-deepseek\vendor\dsh-bridge-work

# 仅在依赖安装期间设置 CI，避免内嵌工程的 postinstall 安装父仓库 Git hooks。
# 不关闭依赖安装脚本；保留原生依赖的正常构建。
$DshPreviousCi = $env:CI
try {
    $env:CI = 'true'
    pnpm install --frozen-lockfile
    if ($LASTEXITCODE -ne 0) { throw 'DSH dependency installation failed' }
} finally {
    $env:CI = $DshPreviousCi
}

pnpm run build:native-system
if ($LASTEXITCODE -ne 0) { throw 'DSH native build failed' }
pnpm exec tsx scripts/compile-referenced-projects.ts host-libraries
if ($LASTEXITCODE -ne 0) { throw 'DSH host build failed' }
```

最后一条是工程已有的 Host 编译与打包入口，包含 Host TypeScript 编译和 tsdown / Typert 生成。`build:native-system` 自带 `--host-addon-only`；Windows 没有对应 system 原生目标时正常跳过，其他平台按源码目标构建。其他原生依赖若无可用预构建包，按安装错误补齐编译环境后重试，不能忽略失败。

小鱼采用 CLI 的 `sdk` profile，不要求为这次聊天迁移构建 DSH Desktop/Web。小鱼 6100 设置网页属于 Python 主项目。确实需要 DSH 完整开发构建时才另执行 `pnpm run build`。

本次源码交付核对了依赖清单、构建入口和打包范围，没有在旧机器重新安装整套依赖或声称已完成全新电脑构建；新机须以以上命令实际成功和 SDK 能启动为准。

## 3. 导入用户另行提供的私有 ZIP

按照 [迁移说明第 4 节](migration-agent-handoff.md#4-解压并导入精简-zip) 解压：`project/` 并入主项目，`external/dsh-home/` 放到专用 `D:\projects\dsh-qq-home`。ZIP 不放入 Git。保留 life.db、会话 ID、人格和 profile 归属标记。

**导入后再改真实 `config.toml` 的机器路径**，否则 ZIP 内旧配置会覆盖新路径：

```toml
dsh_root = "vendor/dsh-bridge-work"
dsh_home = "D:/projects/dsh-qq-home"
```

这些字段位于原配置对应位置，修改原键，不新建重复键。`dsh_node` 改为新机实际 node.exe 绝对路径，可用 `(Get-Command node).Source` 查询。若使用 `dsh_pnpm_entry`，填写实际 `pnpm/bin/pnpm.cjs` 或 `pnpm.mjs` 文件，不能填写 pnpm.cmd；全局 npm 安装可先用 `npm root --global` 定位 pnpm 包，再检查其 `bin/`。同时调整 `dsh_tmp`、插件 link、媒体、咨询等旧电脑路径。

QQ 原图会优先读取平台解析的本地缓存，再尝试公共 QQ CDN 下载。需要缓存读取时，将 `get_image` 实际返回文件所在的 QQ 媒体缓存目录加入 `media_local_roots`；按新机账号和目录填写，不沿用旧机的绝对路径。未配置额外缓存目录时仍可使用 CDN 直连，HTTPS 证书校验保持开启。图片重读和主人本机截图导入会将实际像素送入模型会话。

检查导入的 `dsh-qq-home/profiles/sdk/package.json`：指向旧工程的本地 link 改为新工程中的对应包；外部插件按实际引用恢复。profile 的依赖也需要在新机安装，不能只给主 workspace 安装依赖。保留已有 profile 和状态，不能删除重建来避开缺少插件或路径错误。

Bot 通过 `node --import tsx/esm apps/cli/src/bin.ts --profile sdk` 从所配置的工程根目录启动桥接。正式启动由 Bot 管理专用 home 和进程，不直接运行内部 SDK 应用，也不要同时另起一个 profile 实例。

## 4. 基本验收与后续更新

按迁移说明安装 Python、NapCat 和实际使用的插件后，旧电脑停止 Bot，新电脑启动。先确认设置页能打开、桥接进程正常、SDK 就绪；用户 QQ 登录后用一条普通消息确认能回复。无需为迁移跑全套回归。

以后主仓库更新涉及 DSH 源码时，在停止 Bot 后拉取代码；依赖变化重新执行锁文件安装，并重新构建 Host 后再启动。仅修改主项目文档或私有配置，不需要重复构建 DSH。私有配置、记忆和插件登录态不由 Git 更新。
