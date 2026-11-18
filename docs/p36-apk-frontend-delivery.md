# P36 APK 前端交付报告

> ## ⚠️ 真机未测
>
> **本报告所有"通过"都来自 DOM 桩测试（Node + 真实 `index.html` 脚本），不是真机验证。**
>
> 80 项测试证明的是：**这些脚本在给定输入下会产生预期输出**。
> 它**不**证明：
> - 真机上按钮位置、字体、滚动是否正常
> - Android WebView 与桌面 V8 的行为差异
> - 触控手势（上滑加载、点击条目）是否顺手
> - 无障碍服务真实开启/关闭时的表现
> - `Native.stopPhone` 在真机上的实际效果（**而且这个 APK 还没构建**）
>
> **验收 APK 必须上真机。** 在此之前，只能说明"逻辑正确"。

---

## 交付物

| 文件 | md5 | 字节 |
|---|---|---|
| `mobile/android/assets/index.html` | `0a774c6a…` | 62942 |
| `mobile/android/src/cn/xiaoyu/companion/PhoneService.java` | `8e251e5f…` | — |
| `mobile/android/src/cn/xiaoyu/companion/MainActivity.java` | `dd48371d…` | — |

后端 `life/companion_mobile.py` 的 capabilities 字段由 isolation-verifier 交付（`f52dc54b`），非本报告范围。

## 六项功能与判据

| # | 功能 | 判据 | 测试 |
|---|---|---|---|
| 1 | 历史分页 | 向上滚动拉更早一页，追加到顶部、按 id 去重、不进缓存不串页、视口不跳 | ✅ |
| 2 | 手机操作状态页 | 三层判据分开：本机无障碍 / 能力注册结果 / 云端是否真收到 | ✅ |
| 3 | 停止手机操作 | 与停止语音**双向隔离**；说明"撤不回来"；缺入口时说清更新 | ✅ |
| 4 | 生活条目可聊 | `refs` 协议，不带时请求体与旧版逐字节一致；无 id 不给假入口 | ✅ |
| 5 | 交办事项 | `status` 与 `verification` 分行；缺字段按未核验，绝不显示"核验通过" | ✅ |
| 6 | 更新提示 | `version:null` 不提示；同版本只提一次；忽略旧版仍提示新版 | ✅ |
| 7 | 能力入口 | 状态词原样输出；四档语气；`unknown` 是灰不是红 | ✅ |

## 测试

```bash
node ops/verify-apk-p36.js     # 18 项  手机状态页 + 停止入口
node ops/verify-apk-p36b.js    # 21 项  条目可聊 + 任务产物
node ops/verify-apk-p36c.js    # 17 项  更新提示
node ops/verify-apk-p36d.js    # 24 项  能力入口
```

## 四档状态词的取舍

按**用户读完会采取什么行动**分档，不是按严重程度：

| 状态 | 语气 | 提示 | 用户会怎么做 |
|---|---|---|---|
| `ready` | 绿 | — | 直接用 |
| `needs_setup` | 黄 | （没开启） | 自己去开 |
| `offline` | 红 | （不可用） | 可能来问"怎么坏了" |
| `unknown` | **灰** | （状态未知，不代表坏） | **不该做任何事** |

把 `unknown` 画成红，等于凭空制造一个不存在的问题——比少显示一个能力糟得多。

## 已知限制

1. **真机未测**（见抬头）
2. **APK 未构建**——Java 改动只做了语法级 `javac` 校验（缺 Android SDK，只能验语法不能验链接）
3. 前端验证是逻辑级：跑真实脚本 + 真实 `api()` 桥，不是视觉验证
4. 关键词搜索只在**已加载**条目里筛（服务端暂无搜索接口），代码里已注释说明

## 复现方式

```bash
# 全部 80 项
for f in ops/verify-apk-p36*.js; do node "$f" | tail -1; done
```
