# 浏览器接入 — 实施计划（云端本机版）

日期：2026-11-19（北京时间）
依据：`2026-11-19-evolution-computer-use-integration-design.md`（修订版，云端本机执行）
状态：实施计划，未执行

## 0. 前置评估（已实测，不是推测）

### 环境现状

```
OS          Ubuntu 24.04.2 LTS x86_64
内存        MemTotal 3.42 GiB / MemAvailable 2.11 GiB / SwapTotal 0  ← 无 swap
磁盘        37G 可用
Node        /usr/bin/node v18.19.1（dpkg 包 nodejs）
浏览器      chromium / chromium-browser / google-chrome / microsoft-edge 全无
snap        snapd 2.76 active，/snap 为空，store 里 chromium 可用
AppArmor    apparmor_restrict_unprivileged_userns = 1
            /etc/apparmor.d/chrome 存在，内容含 `userns,` + `flags=(unconfined)`
内存压力    some avg10=0.24 / full avg10=0.08（当前低）
companion   RSS 145 MB
```

### 四个前置问题的可行路径

| # | 问题 | 可行路径 | 风险 |
|---|---|---|---|
| 1 | Node 18 < 要求 22.16 | 装官方 tarball 到 `/opt/node22`，绝对路径调用，不动 `/usr/bin/node` | 低 |
| 2 | 没有 Chromium，apt 只给 snap 包装 | `snap install chromium`，二进制在 `/snap/chromium/current/...` | 中：snap 首次安装要拉 ~200MB |
| 3 | `apparmor_restrict_unprivileged_userns=1` | **写一条 AppArmor profile，路径指向 snap chromium 的真实二进制，内容含 `userns,`**（照抄 `/etc/apparmor.d/chrome` 的形制） | 中：需实测 profile 是否生效 |
| 4 | 内存 2.11 GiB、无 swap | 浏览器空闲 10 分钟退出；独立 cgroup 限 1GiB；启动前检查 ≥1.5GiB | **高：无 swap，OOM 会直接杀进程** |

**关键发现：`/etc/apparmor.d/chrome` 证明了「不破坏沙箱」这条路是存在的——不需要 `--no-sandbox`。**

### 必须先验证再动工的两件事

```
V1. snap 装完 chromium 后，真实二进制路径是什么？能不能 headless 启动？
V2. 给那个路径写 AppArmor profile（带 userns,）后，沙箱能不能起来？
    如果起不来，是 profile 没生效还是别的原因？不能直接跳 --no-sandbox。
```

**判据：V1/V2 任一不通过，停下来报告，不进入 A 阶段。**

## 1. A 阶段：最小真实链路

### 1.1 云端改动（4 个文件）

| 文件 | 改动 | 判据 |
|---|---|---|
| `life/companion.py` | `DECISION.actions.kind` enum 加 `browser`；`_validate_action_projects` 按 op 校验（`tools` 不需 arguments，`call` 需 `tool`）；**不要求 command** | 合法 browser action 过 schema；缺 `tool` 被拒且报字段路径 |
| `life/companion_executor.py` | `available_kinds()` 加 `browser`（条件：适配器可用 + 握手成功） | 不可用时不列出 |
| `life/companion_context.py` | `GUIDES['browser']` + `GUIDES_BRIEF['browser']`，列事实不立规则 | 无「必须/应该」类措辞 |
| 新 `life/companion_browser.py` | 云端适配器：Node 子进程管理、MCP stdio、工具目录、会话租约、action_id 幂等、结果投影 | 见 1.3 |

### 1.2 上游锁版本与补丁（§4.1）

```
锁提交     d11bd4b3c51d31d89d9ee604079e0f6b7103a4a9
版本       0.1.0
补丁内容   ① 根 index 导出 createBrowser（当前没有）
           ② 新增可注入的 Linux 启动函数（绝对路径 + --headless=new + 固定 viewport + 专用 profile）
           ③ 保持 Windows 默认行为不变
补丁形式   在锁定源码上打 patch，构建后记录产物 hash
禁止       运行时改 node_modules；依赖未锁定的内部路径
```

### 1.3 适配器要点

```
进程     一个长驻 Node 适配器；Node + Chromium 在独立 systemd scope/cgroup
浏览器   按需启动，空闲 10 分钟退出（profile 保留）
目录     /var/lib/companion/xiaoyu/data/browser/{profile,procedures,artifacts,requests}
         owner companion:companion；不碰 DSH 或其它 profile
并发     1 个浏览器 / 1 个活动任务 / 最多 2 个标签页 / 排队上限 8
租约     空闲 2 分钟释放任务租约；单任务最长 10 分钟；可配置
幂等     同 action_id 同参数返回已有结果；不同参数拒绝
超时     记录 unknown，不重放
通信     stdio only，不开公网 MCP/CDP 端口
```

### 1.4 握手

```
endpoint_id            cloud-browser
上报内容               运行时版本 / 插件版本 / 浏览器版本 / 工具目录 / 实际可启动状态
能力目录               installed ≠ ready；不可用时给准确原因
```

### 1.5 A 阶段验收（不可跳过）

```
① 真实模型经生产动作入口（/rpc chat → decision → 入队 → executor）
   打开一个公开测试页，读回结构化结果
② 断开浏览器 → 如实说不可用，不谎称已运行
③ 超时 → unknown，不重放
④ 用户中断 → 实际停止
```

**禁止：绕过校验直接调适配器冒充接通。**

## 2. B 阶段：只读流程学习与回放

```
保存       以已成功步骤 + 检查条件保存；状态 saved_unverified
目录       只存引用（procedure_ref / 版本 / 参数摘要 / 验证状态），不复制可写流程
回放       先查目录 → 有合适流程先执行 → 没有则用页面原语
回放校验   唯一入口：通用 call 的 procedure_run、例程入口、旧版回退都过同一校验
只读白名单 按站点/步骤/参数明确列出，配置化，不写死在代码
禁用       首版禁用例程入口；script/提交/发送默认不自动重放
```

**三个验收任务（设计 §11 B）：**
```
① 读取固定页面字段
② 带参数搜索并提取列表
③ 跨页面汇总
```

**每个任务分别记：首次学习的费用/耗时/结果，回放三次的费用/耗时/结果。**

## 3. C 阶段：变化与恢复

```
DOM 改版        明确 failed_step + 页面快照；允许修一次并保存同名新版本
空结果          区分「合法空」与「提取失效」，需任务依据
旧版回退        最多 2 版；标明实际使用版本；最新版仍标失效
进程中断        记录 unknown；同 action_id 不重放
适配进程重连    不自动 resume
用户叫停        实际停止；重启不恢复被停流程
```

## 4. 交付顺序

```
①  V1/V2 前置验证（Node22 + snap chromium + AppArmor profile + headless 启动）
②  上游补丁 + 构建 + hash
③  云端 4 文件改动
④  A 阶段验收（真实模型经生产入口）
⑤  B 阶段三个任务 + 费用表
⑥  C 阶段六项恢复测试
⑦  版本验收清单（每路径一 hash）+ 部署/撤回说明
```

**①不通过则停止，不进入 ②。**

## 5. 必须遵守的边界

```
✗ 不改 phone / 不改现有 computer 坐标语义
✗ 不整体迁入 dsh-world
✗ 不重写现有记忆和调度
✗ 不加 --no-sandbox（沙箱问题修部署）
✗ 不用假模型输出代替真实读页
✗ 不运行时改 node_modules
✓ 模拟契约测试与真实浏览器测试分开报告
✓ 证据分级：代码已实现 / 离线测试通过 / 在线接通 / 真机验收 / 实际发布
```

## 6. 内存风险（无 swap）

```
MemAvailable 2.11 GiB，SwapTotal 0
设计要求的浏览器 + 适配器上限 1 GiB
companion 当前 145 MB，embedding 约 600 MB

最坏情况：浏览高峰 600MB + embedding 700MB + companion 200MB + OS 400MB ≈ 1.9GB
余量很小，且无 swap 兜底。
```

**建议：先加 1-2 GB swap 再部署，否则一次内存峰值会杀掉 companion。**
**这是部署前置条件，不是可选优化。**
