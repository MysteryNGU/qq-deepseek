# P17 修复 APK 能力注册

- 执行者：isolation-verifier（t24）
- 时间：2026-11-17 19:02 CST
- 范围：只改手机端 Java 源码；未动后端 Python、未写生产 life.db、未发真实 QQ、未装到用户手机。

## 0. 先纠正一个前提（重要）

任务书写的是「nginx access log 里**从来没有** `POST /mobile/device`，只有每 20 秒的
`GET /mobile/state`」。**实测不是这样**：

```
总数                    456 条
今天（17/Nov）           78 条
返回码分布              200×441  401×1  404×3  499×8  502×3
最晚几条               17/Nov/2026:18:53:20  200 789
                      17/Nov/2026:18:59:09  200 304
                      17/Nov/2026:19:00:14  200 304
```

也就是说：**`POST /mobile/device` 一直在发，而且大多返回 200**（最近几条是 19:00 前后，
也就是我这次验收开始前不久）。同时 `GET /mobile/state` 也在正常轮询。

**这条会影响验收口径**：本次改动针对的是"异常被静默吞掉、失败不重试、没有定期重报"这些
**真实存在的代码缺陷**，修它们是独立成立的；但"手机上完全发不出注册"这个**现象描述
与日志不符**。可能的原因（我无法在不动后端的前提下确证）：

- `POST /mobile/device` 确实到了服务端并返回 200，但 `PhoneControl.context()` 的 `connected`
  仍然为 false —— 这也可能，因为 `context()` 要求 `device in self.mobile.subscribers`
  （要有一条活的 SSE 连接）**且** `enabled` **且** `operations` 非空。设备注册成功、
  但 SSE 订阅不在，`connected` 一样是 false。
- 或者用户看到的是更早时间段的日志。

**建议**：要判断"她为什么没有 phone 动作"，下一步应当看 `PhoneControl.context()` 的三条件
（subscribers / enabled / operations）而不是只看注册请求。这一条我没有继续查，因为超出
本次"只改 APK"的范围。

## 1. 改了什么（只动两个文件）

备份原件在 `/opt/qq-deepseek/.acceptance/20261117-190220-apk-p17/orig/`：

```
d8b612af…  orig/MainActivity.java
3dfed20b…  orig/PhoneService.java
```

### 1.1 `PhoneService.java`（200 → 278 行）

| 任务要求 | 实现 |
|---|---|
| ① 不再静默吞异常 | 新增 `recordRegister(app, result, detail)`：把 `register_at` / `register_result` / `register_detail`（异常类名+消息，截到 200 字）写进 SharedPreferences。所有失败路径都记 |
| ② 失败后重试 | `for(attempt<REGISTER_RETRIES=3)`，非 200 或异常都重试，退避 `2s→4s→8s`（`REGISTER_BACKOFF_MS<<attempt`） |
| ③ 定期重注册 | 每次注册结束（成功或彻底失败）都 `main.postDelayed(register, REGISTER_INTERVAL_MS=20000)`，与消息连接 20 秒节奏一致 |
| ④ 保证基础能力送达 | `PhoneWorkspaceManager` 的三处调用各自包 `try-catch(Throwable)`：`available()` / `state()` / `capabilities()` / `lastError()` 失败只丢对应字段，`enabled` + `operations` + `coordinate_space` 用独立的 `baseOperations()` 先建好，绝不因可选字段整体失败 |
| ⑤ 改动保守 | 未改类结构、未改其它方法、未改签名/权限配置 |

另外：`if(!Api.paired(app))` 这条早退现在也会 `recordRegister("unpaired", ...)`，
这样"没配对"这件事在诊断接口里也看得见，而不是静默 return。

**保留的 `catch(Exception ignored){}`（5 处）**：都在 `register()` **之外**
（`report()` 的控制回执、`onServiceConnected()` 读 prefs、`onDestroy()` 的 event 广播等）。
那些不是本缺陷所指的路径，为保守起见未动。

### 1.2 `MainActivity.java`（255 → 260 行）

`Bridge.config()` 的 JSON 增加四个字段，用来直接看手机卡在哪一步：

```json
"register_at": <毫秒时间戳>,
"register_age_ms": <距上次注册尝试多久，0 表示还没试过>,
"register_result": "ok" | "unpaired" | "partial" | "failed" | "http_<code>",
"register_detail": "attempt=1 …" / 异常类名+消息
```

## 2. 构建与产物

- 构建命令：`python3 tools/build-companion-android.py`（**未改构建链**）
- 构建结果：**成功**，编译期只有既有 warning（`bootstrap class path not set`、
  `androidx.annotation.RestrictTo$Scope not found` 等 8 条，改前同样存在）

| 项 | 值 |
|---|---|
| **APK 绝对路径** | `/opt/qq-deepseek/data/android-build/output/Xiaoyu-0.4.7.apk` |
| **sha256** | `4136347a6eaa7525163a14e40bcd302ef5d73cf30d7d934d077c85a77fc3570e` |
| 大小 | 176939 字节 |
| **versionName / versionCode** | `0.4.7` / `14`（沿用，未改） |
| package | `cn.xiaoyu.companion` |
| minSdk / targetSdk | 26 / 35 |
| **签名** | `apksigner verify`：**Verifies**；v2=true、v3=true；签名者 `CN=Xiaoyu Companion`，证书 SHA-256 `fb83594cab08da30e271cd81be8d1b35f581d60e4e39ab695b00662dc48bb6ce` |
| 签名配置 | **未改**（沿用既有 keystore；只有同一个 key 才能覆盖安装） |

同目录另有 `Xiaoyu-0.4.7.apk.idsig`。产物与源码已归档到
`/opt/qq-deepseek/.acceptance/20261117-190220-apk-p17/`（`apk/`、`*.java`、`*-SHA256SUMS.txt`）。

### 2.1 证明"改的代码真进了 APK"

不只看编译通过——直接从 APK 里解出 `classes.dex` 的字符串表核对：

```
strings classes.dex | grep -x
  register_result : 1        register_detail : 1
  register_at     : 1        register_age_ms  : 1
  workspace_state : 1
对照（改前就有的字符串，证明方法有效）：
  phone_seen : 1             coordinate_space : 1        workspace_error : 1
```

> 期间踩了一个坑并已排除：先用 `grep -a` 数 dex 得到 0，那是二进制匹配的假象；
> 换 `strings -x` 精确匹配后为 1。

## 3. 验证到了哪一步（如实）

| 要求 | 状态 |
|---|---|
| 编译成功 | ✅ |
| `apksigner verify` 通过 | ✅ Verifies，v2/v3 均 true |
| 新代码确实打进 APK | ✅ dex 字符串表逐项命中 |
| 签名未换、版本号未改 | ✅ 证书 SHA-256 与既有 key 一致，versionCode 仍 14 |
| 未装到用户手机 | ✅ 只产出 APK 文件，未执行任何 adb/install |
| 未动后端 Python / 生产库 / 真实 QQ | ✅ 只改了两个 `.java` |

### 仍待验证（不能由我完成）

1. **装上新 APK 后，云端是否出现更稳定的 `POST /mobile/device`**——需要用户自己安装。
2. **`PhoneControl.context().connected` 是否因此变 true**。按 §0 的分析，注册请求本来就在发、
   且多为 200，所以**光靠这次改动不一定能让 `connected` 变 true**；还要确认 SSE 订阅
   （`mobile.subscribers`）与 `enabled`/`operations` 三项同时成立。**这条我建议单独查**。
3. **手机上真的卡在某一步时，`config()` 能否显示出来**——需要装上后打开 App 复现一次故障才看得到。
4. 我没有设备，**无法验证**重试/退避/20 秒重报在真机上的实际时序。

## 4. 回滚

```sh
cp .acceptance/20261117-190220-apk-p17/orig/PhoneService.java \
   .acceptance/20261117-190220-apk-p17/orig/MainActivity.java \
   mobile/android/src/cn/xiaoyu/companion/
python3 tools/build-companion-android.py
```

## 5. 一句话

代码按五条要求改完、编译通过、签名校验通过、新字符串确认进了 dex；
**但任务书里"从来没有 POST /mobile/device"这个前提与日志不符**（456 条、今日 78 条、多为 200），
所以这次修的是"异常被吞、不重试、不重报"这些**确定的代码缺陷**，
**不能据此断言"装上就能连上"**——`connected` 还取决于 SSE 订阅与 abilities 是否齐全，建议单独排查。
