# APK 六项缺口审计（P29）

只读审计。目标不是给方案，是给**每条缺口的真实边界**：它现在断在哪个文件哪一行、
服务端到底有没有这个数据、补它要动前端还是必须动服务端。

> **引用约定**：本文所有「文件:行号」用带路径的写法，例如
> `mobile/android/assets/index.html:154`、`life/companion_mobile.py:299`。
> 原因是仓库存着同名文件（`life/settings_ui/index.html`、
> `tools/stage-c/MainActivity.java` 等），裸文件名指不到唯一位置。
> 自检命令：`python -B -X utf8 tools/verify-doc-line-refs.py docs/apk-gap-audit.md`

**审计基线**

| 项 | 值 | 怎么确认的 |
|---|---|---|
| 源码位置 | 云端 `/opt/qq-deepseek/mobile/android` | 本机 `mobile/android` 有同名副本 |
| 发布包 | `/var/www/companion/downloads/Xiaoyu-0.4.7.apk` | — |
| 发布包 vs 源码 | **一致**（`mobile/android/assets/index.html`、`mobile/android/assets/mobile-input.js` 字节级相同） | 解包比 sha256：`mobile/android/assets/index.html` 37056B `0a267e61…`，`mobile/android/assets/mobile-input.js` 11437B `0e144f45…` |
| 本文件行号 | 对**源码和 0.4.7 发布包同时有效** | 同上 |
| 服务端源码 | `/opt/qq-deepseek/life/companion_mobile.py`、`companion_phone.py` | 云端运行中 |

审计方法：源码通读 + 云端实测（只读查询运行时数据 `data/companion/mobile.sqlite3` 与
`data/companion/mobile-devices.json`，都不在仓库里，
直连 `http://127.0.0.1:8765` 打真实鉴权路径）。**下面每一条的「实测」都是真跑出来的，
不是读代码推出来的**；凡是我没能实测的，明确写「未实测」并说明原因。

---

## 总表：改前端还是改服务端

| # | 缺口 | 结论 | 最小改动面 |
|---|---|---|---|
| 1 | 消息状态不可见 | **改前端即可**（服务端数据已在，只是前端不读） | `mobile/android/assets/index.html` + `mobile/android/assets/mobile-input.js`；服务端零改动 |
| 2 | 草稿不持久 | **改前端即可** | `mobile/android/assets/index.html`（localStorage）+ `mobile/android/src/cn/xiaoyu/companion/MainActivity.java`（可选加固） |
| 3 | 历史只有 60 条 | **必须改服务端**（前端没有别的地方能拿更多） | `companion_mobile.py`：`HISTORY_LIMIT` + 分页参数 |
| 4 | 401 无法区分 | **两端都要改**：服务端先给可区分的错误码，前端才能照实说 | `companion_mobile.py` 鉴权 + `mobile/android/assets/index.html:49` 文案 |
| 5 | 运行详情缺陷 | **改前端即可**（两行） | `mobile/android/assets/mobile-input.js:124`、`:127` |
| 6 | 手机操作状态页 | **两端都要改**：服务端要暴露注册结果，前端要读 | `companion_phone.py` + `mobile/android/assets/index.html:157` |

一句话概括：**1、2、5 是纯前端**（服务端早就有数据，前端没读）；
**3 是纯服务端**（数据被服务端砍掉了，前端无从补救）；
**4、6 必须两端一起**（服务端不给区分依据，前端编不出来）。

---

## 1) 消息状态不可见

**用户原话**：发出一段话，没有回复；不知道她还在想、处理失败了，还是需要重新发送。

### 完整事件链（实测走通）

```
index.html:154 send()
  → api('POST','/mobile/chat',{id: uuid})
  → companion_mobile.py:306 chat()        ← 落库 accepted，202 立刻返回
  → companion_mobile.py:320 spawn(_chat)  ← 真正处理在后台
  → _chat(): 调 core.chat() 真跑
  → finally: UPDATE requests SET status=? ← completed / failed / interrupted
  → emit(type='mobile_request', id, status) ← 前端订阅了却没装机
```

上面这条链是**读代码走通的**（服务端 306→320→323→335 连续，前端 154 对应）。
真跑的证据是 `requests` 表里的终态分布（见下）：253 条消息确实走完了整条链并落到终态。

逐环节的代码位置：

| 环节 | 位置 | 行号 |
|---|---|---|
| 生成消息 id | `mobile/android/assets/index.html` | 153 `uuid()` |
| 发出 | `mobile/android/assets/index.html` | 154 |
| 服务端收下、落库 `accepted`、返回 202 | `companion_mobile.py` | 319–321 |
| 异步处理 | `companion_mobile.py` | 323–335 |
| 写终态 | `companion_mobile.py` | 334 |
| 推事件 `mobile_request` | `companion_mobile.py` | 335 |
| 服务端把状态暴露给手机 | `companion_mobile.py` | 299 |

### 前端在哪一步丢了状态

**三处，逐个说：**

**① HTTP 层只认「收下了」**
`companion_mobile.py:321` 返回 `202 {accepted:true}`。`mobile/android/assets/index.html:154` 的 `await api(...)`
拿到 202 就当成成功，立刻清空输入框：

```js
await api('POST','/mobile/chat',{id:lastRequest.id,text,images:sentImages});
lastRequest=null;                       // ← 154 行，此时服务端还没开始处理
if($('text').value.trim()===text){$('text').value=''}   // ← 输入框当场清空
```

202 的语义是「我已收下，结果另说」。前端把它当成了「她收到了」。

**② 服务端给了状态，前端从来没用**
`companion_mobile.py:299` 每次 `/mobile/state` 都会带：

```python
snapshot['mobile_requests']=[dict(id=row[0],status=row[1]) for row in self.db.execute(
    'SELECT id,status FROM requests WHERE device=? ORDER BY created DESC LIMIT 30',(device,))]
```

**实测：前端代码里 `mobile_requests` 零引用。** 我 grep 了 `mobile/android/assets/index.html` 和
`mobile/android/assets/mobile-input.js` 全文，只在 `mobile/android/assets/index.html:137` 的 `onServerEvent` 里出现过
`mobile_request`（事件名，用于触发 `queueRefresh`），**没有任何地方读
`state.mobile_requests`**。服务端把状态送到门口了，前端没开门。

**③ `mobile_request` 事件被当成「该刷新了」**
`mobile/android/assets/index.html:137`：

```js
if(['message','ready','resync','snapshot','status','action_result','mobile_request'].includes(e.type)){
  queueRefresh(); ...
}
```

`mobile_request` 在这里唯一的作用是**触发一次刷新**，它携带的 `id` 和 `status`
被直接丢弃。所以即使服务端喊「这条失败了」，前端只会重画一遍历史，用户看到的还是
「我发了、她没回」。

### 现有 `companion_actions` 台账能不能直接用？

**不能，也不该用。** 理由有三，都有实测依据：

1. **粒度不对。** `companion_actions` 记的是「她执行的动作」，一条用户消息可能触发
   0 个动作（纯聊天）或 N 个动作（连着做几件事）。用户问的是「我这句话她处理到哪了」，
   这个问题由 `requests` 表回答，不由 `companion_actions` 回答。

2. **数据在客户端已经够用。** 手机这边真正需要的终态就在 `mobile.sqlite3` 的
   `requests` 表里。**实测**（云端只读查询）：

   ```
   completed   227
   failed       24
   interrupted   2
   accepted      0     ← 当前没有卡在处理中的
   ```

   也就是说，**26 条真出过问题的消息，用户当时全都没有任何提示**。
   这 24 + 2 条正是「她没回」的真实来源。

3. **字段已经够，只是少了两样。** 现有 `requests` 表是
   `(device, id, digest, status, created, updated)`。渲染「发了 / 她在想 / 失败了」
   需要 `status` 和 `updated`——**两个都有**。`/mobile/state` 现在只取
   `id, status`（299 行），**把 `updated` 丢了**。补这一列是一行 SQL 的改动，
   而且是在「本来就该给」的字段上，不是新造概念。

### 结论

- **改前端即可解决主体问题。** 前端已有全部所需数据（`mobile_requests` + `phase`），
  零服务端改动就能显示「已送达 / 她在想 / 处理失败」。
- **建议顺带补一个字段**（服务端一行）：299 行的 SELECT 加 `updated`，
  让前端能显示「已等待 N 秒」。这是**可选增强**，不加也能解决用户的原话问题。
- **`companion_actions` 不适合作这个消息状态的来源**，上面第 1 点已说明。

---

## 2) 草稿不持久

### 现在的内存态都在哪

| 内存态 | 位置 | 生命周期 |
|---|---|---|
| 输入框文字 | `mobile/android/assets/index.html:28` `<textarea id="text">` 的 DOM value | 页面重载即失 |
| 待发图片 | `mobile/android/assets/index.html:35` `let images=[]` | 页面重载即失 |
| 幂等键 | `mobile/android/assets/index.html:35` `let lastRequest=null` | 页面重载即失 |

### Android 侧实际保存了什么

`mobile/android/src/cn/xiaoyu/companion/MainActivity.java:148`：

```java
@Override protected void onSaveInstanceState(Bundle state){
    super.onSaveInstanceState(state);
    if(cameraUri!=null)state.putString("camera_uri",cameraUri.toString());
}
```

**只存了 `cameraUri`，即「相机正在拍的那张照片的临时 URI」。**
输入框、已选图片、未完成请求，一个都没存。

`onCreate` 也只恢复这一个（`mobile/android/src/cn/xiaoyu/companion/MainActivity.java:46`）。

### 已有的一半能力

`mobile/android/assets/index.html:133-134` 已经在用 localStorage 缓存状态：

```js
const STATE_CACHE='xiaoyu.state.v1';
function storeState(state){try{localStorage.setItem(STATE_CACHE,JSON.stringify({history:...(state.history||[]).slice(-60),...}))}catch(e){}}
```

而且 `mobile/android/src/cn/xiaoyu/companion/MainActivity.java:74` 已经开了 `setDomStorageEnabled(true)`。
**localStorage 是可用的，只是没拿来存草稿。**

### 最小侵入的持久化点

**首选：纯前端，在 `mobile/android/assets/index.html` 用 localStorage。**
三个建议落点，都是现成函数旁边的几行：

| 要保的 | 建议落点 | 为什么是这里 |
|---|---|---|
| 输入框文字 | `mobile/android/assets/index.html:155` 的 `$('text').oninput` 里 | 已有一次输入回调，加一行 `localStorage.setItem` 即可 |
| 待发图片 | `mobile/android/assets/index.html:152` `onPickedImage` 和 151 `attachmentUI` | 图片数组唯一的两个写入口 |
| 未完成请求 | `mobile/android/assets/index.html:154` | `lastRequest` 生成处；它本来就是幂等键，重发时复用才不会重复 |

**注意一个真实的坑**：图片是 base64（`mobile/android/src/cn/xiaoyu/companion/MainActivity.java:256`，单张上限 8MB，
`mobile/android/assets/index.html:152` 最多 8 张）。整组塞进 localStorage 可能几十 MB，会撞配额。
建议草稿只存**文字**，图片重启后丢弃——这与「最小侵入」一致，也避免配额异常。

**可选加固（需要改 Android）**：如果希望连「正在拍的那张照片」之外的进程被杀的
恢复也可靠，可在 `mobile/android/src/cn/xiaoyu/companion/MainActivity.java:148` 的 `onSaveInstanceState` 里补
`state.putString("draft_text", ...)`。但这要求把 WebView 里的文字取出来，
比 localStorage 方案重得多——**推荐先只做前端方案**。

### 结论

**改前端即可。** 已有 `localStorage` 和 `setDomStorageEnabled(true)` 两个前提条件，
不需要动 Android 代码。`onSaveInstanceState` 目前只保 `cameraUri`，是事实，但它
不是草稿丢失的原因——真正的原因是 WebView 每次启动都重新 `loadUrl` 一次空白页面。

---

## 3) 历史只有 60 条

### 限制的确切位置

服务端**两道**截断，串联生效：

**第一道：`companion_mobile.py:66`**

```python
HISTORY_LIMIT = 60
```

用在 `companion_mobile.py:89`：

```python
for row in (snapshot.get('history') or [])[-history_limit:]:
```

**第二道：服务端快照本身就有上限**
`life/companion.py:3276` 调 `self._recent(...)`，而 `_recent` 的签名是
`def _recent(self, limit=100, ...)`。所以 `/mobile/state` 拿到的 `history`
**最多 100 条，再被切到 60 条**。

也就是说：即使把 60 改大，超过 100 也不会再有，除非一起改 `_recent` 的默认值。

### 能不能加参数分页而不破坏现有行为

**能，而且很干净。** `mobile_view` 已经接受 `history_limit` 参数（81 行签名），
现在是固定传默认值。分页在结构上是现成的：

- 现有 `GET /mobile/state` 无参数 → 行为不变（默认 60）。
- 新增 `GET /mobile/state?history_before=<timestamp|id>&history_limit=N` →
  `mobile_view` 多一个「取这段之前的」过滤。
- 前端 `mobile/android/assets/index.html:134` 的 `storeState` 已经在存 60 条（`.slice(-60)`），
  说明前端本来就按 60 设计；分页要同时放宽它。

**未实测的部分（说明理由）**：我没有真的发带分页参数的请求去验证，
因为**服务端目前没有这个参数**——发了也只会被忽略。这一条是「结构上可行」的
判断，基于上面三处代码事实，不是端到端验证过的结论。

### 结论

**必须改服务端。** 60 这个数字写死在服务端（`companion_mobile.py:66`），
前端无论怎么改都拿不到第 61 条。第二道上限在 `life/companion.py` 的
`_recent(limit=100)`，改前端没用。

---

## 4) 401 无法区分

### 所有返回 401 的路径

**实测**（直连云端 `127.0.0.1:8765`，逐条打真实鉴权路径）：

| 场景 | 实际返回 |
|---|---|
| 完全不带 Authorization | `401: Unauthorized` |
| `Bearer `（空 token） | `401: Unauthorized` |
| 从未配对过的随机 token | `401: Unauthorized` |
| 前缀不是 `mob_` | `401: Unauthorized` |
| 拿主人 token 打手机接口 | `401: Unauthorized` |
| 配对码错误 | `401: Pairing code invalid or expired` |

**除了配对码那条，其余五条的响应体一字不差。** 客户端在物理上没有区分依据。

代码位置 `companion_mobile.py:252-272`，具体 401 出自：

- `:258` — `/mobile/pairing` 缺服务端 token
- `:264` — `/mobile/link` 缺服务端 token
- `:267` — 没有 `Bearer ` 前缀
- `:270` — **四种情况合并成同一句**：token 不是 `mob_` 开头 / 查不到设备 /
  设备已被 `revoked` /（正常的）token 不匹配。原文：

  ```python
  if not token.startswith('mob_') or not device or device.get('revoked'):raise web.HTTPUnauthorized()
  ```

### 能否区分「token 过期」「设备被移除」「从未配对」

**现状：三条都区分不了，其中两条服务端甚至没有数据能区分。**

| 想区分的情况 | 服务端有依据吗 | 结论 |
|---|---|---|
| 从未配对 | **没有。** 服务端只存已配对设备的 token 哈希，查不到 ≠ 没配过 | **做不到**（除非改数据结构） |
| 设备被移除 | **有。** `devices` 里有 `revoked` 字段（`companion_phone.py` 与配对逻辑使用），只是被合并进同一个 401 | **能区分**，需要拆开 |
| token 过期 | **没有。** `companion_mobile.py:287-288` 发放 token 时只写 `id / name / created_at`，**没有任何过期时间**，运行时 `data/companion/mobile-devices.json` 里也没有 `expires` 字段 | **做不到**——现在根本不存在「过期」这回事 |

**「token 过期」在当前实现里是一个不存在的情形。** 实测云端
运行时 `data/companion/mobile-devices.json` 只有 `id / name / created_at`（+ 可选的 `revoked`），
没有过期时间。

### 前端现在的说法

`mobile/android/assets/index.html:49`：

```js
p.reject(Error(status===401?'配对码无效或已用过，请检查后重试。':...))
```

**这是一个会误导用户的文案。** 只有真正在配对时才可能「配对码无效」；而已经配对过的
用户遇到 401（比如设备被移除），看到的却是「配对码无效或已用过」——他会去重输配对码，
而正确做法是重新配对或联系主人。

### 结论

**两端都要改。**
- **服务端必须先给区分依据**：把 `:270` 那一句拆成至少三种不同的错误码
  （例：`device_revoked` / `token_unknown` / `malformed_token`）。这一步前端无法代劳。
- **前端再照实说**：`mobile/android/assets/index.html:49` 按错误码换文案。
- **「token 过期」要实现就得先在服务端加过期字段**——这超出了文案范围，是功能新增。
  如果不想引入过期机制，就该**从需求里去掉这一条**，否则前端只能继续编。

---

## 5) 运行详情缺陷（已确认）

用户说法是「发布包 line 124 的按钮处理函数没传参数」。**已在源码里核实，
并且确认 0.4.7 发布包的 `mobile/android/assets/mobile-input.js` 与源码字节一致，所以行号对得上。**

### 确切位置

`mobile/android/assets/mobile-input.js:124`：

```js
$('openRuntime').onclick=()=>{document.activeElement?.blur();$('overlay').style.display='none';
  $('runtimeOverlay').style.display='flex';renderObservability(latestState);refresh()};
```

`mobile/android/assets/mobile-input.js:127`（同一个毛病）：

```js
$('refreshRuntime').onclick=()=>refresh();
```

### 调用链

```
refresh(runtime)                        index.html:136
  runtime = undefined                   ← 两处调用都没传参
  → wants = !!runtime || runtimePending → false
  → GET /mobile/state                   ← 不带 ?runtime=1
  → 服务端 mobile_view(runtime=False)
  → view 里没有 'observability'          companion_mobile.py:130-131
  → renderObservability(state) 命中 obs 缺失分支
  → 渲染成「当前服务没有提供详细运行记录」 mobile-input.js:62-66
```

服务端逻辑本身是对的（`companion_mobile.py:130`：`if runtime: view['observability']=...`）。
**问题纯粹是前端没要。** 打开「运行详情」永远显示「服务没有提供详细运行记录」，
而实际上服务端有，只是没被请求。

### 对照：唯一正确传参的地方

`mobile/android/assets/index.html:137`：

```js
if(e.type==='phase'){phaseRevision++;applyPhase(e.phase||{});if($('runtimeOverlay').style.display==='flex')refresh(true)}
```

**只有收到 phase 事件时才会 `refresh(true)`。** 用户静止不动时，运行详情永远是空的。

### 确切修改点

- `mobile/android/assets/mobile-input.js:124` → `refresh()` 改成 `refresh(true)`
- `mobile/android/assets/mobile-input.js:127` → `refresh()` 改成 `refresh(true)`

### 结论

**改前端即可，两行。** 服务端零改动。

---

## 6) 手机操作状态页

### 无障碍已开启时，「云端注册成功」的真实判据

**不能看系统设置。** `Settings.Secure` 里的无障碍开关只说明用户点了「开启」，
不代表「小鱼 · 手机操作」这个服务真的连上了、也不是云端认可的状态。

真实判据分两层，**必须同时成立**：

**第一层（设备侧）**：`PhoneService.current != null`
`mobile/android/src/cn/xiaoyu/companion/PhoneService.java:42`：`static boolean enabled(){return current!=null;}`

`current` 在这个服务被系统真正绑定成功后才赋值（`mobile/android/src/cn/xiaoyu/companion/PhoneService.java:155-156` 的
`onServiceConnected(){current=this;}`），`onDestroy` 时清空（`mobile/android/src/cn/xiaoyu/companion/PhoneService.java:163-165`）。
**这是「服务真的跑起来了」，不是「设置里显示已开启」。**

**第二层（云端侧）**：`/mobile/device` 注册返回 200，云端 `PhoneControl.devices[id]`
里 `enabled=True` 且 `operations` 非空。

云端判定在 `companion_phone.py:93-100`（`register`）与 `:70-79`（`context`）：

```python
self.devices[device]=dict(enabled=value.get('enabled') is True, ...,
    operations=sorted(set(operations)&(OPS|WORKSPACE_OPS)), ..., last_seen=now)
```

`context()` 只把「注册窗口内 + `enabled` + `operations` 非空」的设备算作在线（`:73`）。
**所以「云端注册成功」的真正判据是：`context()` 的 `devices` 列表里出现这台手机，
且它的 `operations` 非空。** 只看 `enabled` 不够——`operations` 为空时云端照样不派活。

**注册结果本来就写在设备上**：`mobile/android/src/cn/xiaoyu/companion/PhoneService.java:48-54` 的 `recordRegister()` 把
`register_at` / `register_result` / `register_detail` 写进 SharedPreferences，
`mobile/android/src/cn/xiaoyu/companion/MainActivity.java:176-178` 已经通过 `config()` 把它们暴露给前端了。

**但现在前端只用了一个布尔值**——`mobile/android/assets/index.html:157`：

```js
function phoneStatus(){const cfg=config();
  $('phone').textContent=cfg.phone_enabled?'已开启 · 查看':'打开授权页';
  ...
```

`phone_enabled` 来自 `mobile/android/src/cn/xiaoyu/companion/MainActivity.java:172` 的 `PhoneService.enabled()`，即第一层。
**第二层（注册结果 `register_result` / `register_detail`）已经在 `cfg` 里，
前端一个字都没显示。**

### 现有停止入口

| 停止什么 | 入口 | 位置 |
|---|---|---|
| 停止语音播放 | `Native.stopSpeech()` | `mobile/android/assets/index.html:155` 输入时、`:154` 发送时、`mobile/android/assets/mobile-input.js:15` 点击语音状态条时 |
| 停止手机操作 | **不存在前端入口** | 只能去系统设置关无障碍服务 |

`mobile/android/src/cn/xiaoyu/companion/MainActivity.java:204`：`stopSpeech(){runOnUiThread(()->speech.stop());}` ——
**只有语音，没有「停止手机操作」的桥方法。**

### 能否区分「停止语音播放」和「停止手机操作」

**现状：天然区分，因为后者根本没有入口。**

- 语音：有独立状态条（`mobile/android/assets/index.html:28` `#speechStatus`）+ 点击停止（`mobile/android/assets/mobile-input.js:15`）
  + 自动停止时机。语义清楚。
- 手机操作：**没有停止入口**。用户想让它停，只能进系统设置关无障碍。

**要把「停止手机操作」加进来，必须先定义它停什么**——这是产品问题，不是实现问题：

| 候选含义 | 难点 |
|---|---|
| 停掉正在执行的那一个动作 | 需要在设备侧维护「当前动作」并可取消；云端 `companion_phone.py` 要能撤回已派发的指令 |
| 停掉「让小鱼操作手机」这个开关 | 等于本地拒绝后续派活，但服务仍连着；云端需要知道并停止派发 |

两者的区别**不能只靠前端做**：前端没有任何「当前是否有手机动作在跑」的数据。
`/mobile/state` 里没有正在执行的手机动作字段，`phone_context` 是给角色的，
不是给界面用的。

### 结论

**两端都要改，而且要先定语义。**
- **第一层状态已够用**：`config().phone_enabled` 现在就能显示，前端只是没用全。
  要显示「注册成功/卡在哪一步」，`register_result`/`register_detail`
  **已经在 `config()` 里**，**改前端即可**，服务端零改动。
- **「云端认可的手机操作可用」不能只看 `phone_enabled`**，要看云端
  `context()` 里的 `devices`（`companion_phone.py:70-79`）。前端目前拿不到这份数据——
  若要在手机上显示，需要**服务端**在 `/mobile/state` 里暴露手机自己的注册态。
- **「停止手机操作」是功能新增**，服务端和 Android 都要动，且必须先确定语义。
  **不要把它和「停止语音播放」放进同一个按钮**——两者现在一个有计划、一个没有，
  混在一起会让用户以为关掉了手机操作，其实没有。

---

## 附：本审计没能实测的部分

如实列出，避免被当成已验证结论：

1. **分页参数**（第 3 条）：服务端没有该参数，无法端到端验证。结论基于三处代码事实。
2. **token 过期**（第 4 条）：系统里不存在过期机制，无法实测一个不存在的状态。
3. **真机无障碍开启/关闭**：我没有手机，无法实测 `PhoneService.current` 的变化。
   第 6 条第一层判据来自代码（`mobile/android/src/cn/xiaoyu/companion/PhoneService.java:42`、`:155-156`、`:163-165`），非真机观测。
4. **0.4.7 包内 `classes.dex` 与源码的一致性**：只比对了 Web 资源
   （`mobile/android/assets/index.html`、`mobile/android/assets/mobile-input.js`）字节一致；Java 部分未反编译比对，
   因此第 2、6 条涉及的 Java 行为以源码为准，未在发布包内验证。
