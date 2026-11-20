# 现有 13 种工具的可用性测试（P37 / t18）

日期：2026-11-18 16:17–16:50 CST　执行：isolation-verifier
被测版本：`life/companion.py = 6a2749b3…`、`companion_context.py = 6c6756e5…`、
`companion_awareness.py = dab40931…`、`companion_work.py = c3e42a5b…`（均为服务实际加载的那份）
模型：`deepseek-v4.1-flash`（opencode-go）

**没有改任何产品代码。** 所有结论来自：①部署字节里的说明原文 ②真 `/rpc` 对话
③`companion_actions` / `companion_agent_tasks` 台账 ④她自己的 `self_output`。

---

## 0. 一句话结论

**13 种全部向她暴露了，12 种在生产上确有真实成功回执，`computer` 一次都没连上过。**
但**说明与实际实现有一处硬矛盾**（`work.run`），以及**一个结果语义陷阱**
（`status=completed` 里混着 `outcome=incomplete`）——这两处都不是"工具缺失"，
是"能力发现与理解链路"的问题。

---

## 1. 四层证据表

图例：**✅** 有实测证据　**⚠️** 有证据但有问题　**❌** 明确不可用　**➖** 无法验证（说明原因）

| # | kind | ① 暴露 | ② 说明 | ③ 可达 | ④ 结果可理解 |
|---|---|---|---|---|---|
| 1 | `command` | ✅ `GUIDES['command']` L31 | ✅ 与实际一致（见 §3.1） | ✅ 今日 50 次，48 completed | ✅ 退出码非 0 → `status=failed`，原文可读 |
| 2 | `work` | ✅ `GUIDES['work']` L32 | ⚠️ **承诺 `run` 但未注册**（§2.1） | ✅ 43 次，40 completed | ✅ `edit` 回执带 sha/字节数，她据此复验过 |
| 3 | `dsh` | ✅ `GUIDES['dsh']` L33 | ✅ 与实际一致 | ❌ 今日 11 次仅 2 completed，9 failed | ⚠️ 失败原因含糊（§2.3） |
| 4 | `agent` | ✅ `AGENT_INSTRUCTIONS` companion.py L344-356 | ✅ 七 op 与 `OPS` 完全一致 | ⚠️ 20 次：3 completed / 11 failed / 6 cancelled | ⚠️ **`completed` 里混 `outcome=incomplete`**（§2.2），但她自己识破了 |
| 5 | `computer` | ✅ `GUIDES['computer']` L34 | ✅ 与实际一致 | ❌ **台账 0 行，历史上从未调用过** | ➖ 无法验证（从未执行） |
| 6 | `body` | ✅ `GUIDES['body']` L36 | ✅ 与实际一致 | ⚠️ 50 次：38 completed / 12 failed | ✅ 失败回执「桌面身体尚未连接」她读得懂 |
| 7 | `bilibili` | ✅ `GUIDES['bilibili']` L37 | ✅ 与实际一致 | ✅ 50 次：40 completed | ✅ 视觉回执带时间点/覆盖范围/局限，她据此说「只看了抽样」 |
| 8 | `phone` | ✅ `GUIDES['phone']` L35 | ✅ 与实际一致 | ⚠️ 50 次：26 completed / 23 failed | ✅ 失败有具体原因 |
| 9 | `town` | ✅ `GUIDES['town']` L29 | ✅ 与实际一致 | ✅ 50 次全部 completed | ✅ 回执明确「交办已登记，尚不能声称已行动」（§2.4） |
| 10 | `web` | ✅ `GUIDES['web']` L25 | ✅ 与实际一致，**且写了本机可达性** | ✅ 50 次：34 completed / 16 failed | ✅ 摘要标 `read_scope=search_snippet` 并提示「不是原文」 |
| 11 | `skill` | ✅ `GUIDES['skill']` L26 | ✅ 与实际一致 | ✅ 5 次：2 completed（本次实测 1 次成功） | ✅ 回执带 `evidence_note`：「这是方法原文，不是你执行过它的证据」 |
| 12 | `memory` | ✅ `GUIDES['memory']` L27 | ✅ 与实际一致 | ✅ 29 次：24 completed | ✅ 明确区分 `never_used` 与「做不到」 |
| 13 | `growth` | ✅ `GUIDES['growth']` L28 | ✅ 与实际一致 | ✅ 9 次：8 completed | ✅ 回执自己说明账本从何时开始记 |

**暴露（①）：13/13 全部有说明。** 12 条在 `companion_context.GUIDES`，`agent` 单独在
`companion.py:344` 的 `AGENT_INSTRUCTIONS`——它也进了渲染文本（实测 `tool_instructions()`
输出 5395 字符，13 个 kind 名字全部出现）。

---

## 2. 发现的真问题

### 2.1 【硬矛盾】`work` 的说明承诺了 `run`，实现里没有，而且错误消息自相矛盾

说明原文（`companion_context.py:32`）：

> …`grep{pattern,path,include?}`，**`run{command,cwd?}`走主机shell**。…
> **改完必须再用read或run复验。**

实际 `companion_work.py:234`：

```python
handlers = {'read': self.read, 'write': self.write, 'edit': self.edit,
            'list': self.list, 'glob': self.list, 'ls': self.list, 'grep': self.grep}
```

**没有 `run`。** 而且第 238 行的错误消息把它自己也写进了允许列表：

```
op 只能是 read/write/edit/list/grep/run，收到 'run'
                                          ↑ 它刚刚被拒绝
```

实测（直调部署模块）：

```
run (guide promises it)
  WorkError: op 只能是 read/write/edit/list/grep/run，收到 'run'
```

**影响**：`work` 的「改完必须再用 read **或 run** 复验」这句指导，其中一半是走不通的。
她想用 `work.run` 跑一条命令复验时，会收到一条**告诉她 run 是合法的**、却拒绝 run 的消息。

**为什么之前没人发现**：`work` 的 43 次真实调用里 40 次成功，用的都是 read/edit/list/grep
——**说明里那句 `run{command}` 从来没被真正执行过**。她实际复验时走的是 `command` kind
（台账里 117 次 command），所以链路碰巧是通的。

> 这条和 t11 的 `node=work` 是同一族：**说明描述了一个不存在的后端**。

### 2.2 【语义陷阱】`agent` 的 `status=completed` 里混着 `outcome=incomplete`

`companion_agent_tasks` 实测：

```
753f757c  status=completed  verification=pending  node=local origin=dsh
          outcome='incomplete'
          result: 「已完成：已生成并验证文件 hello-from-dsh.txt…没有未完成事项」
1a1ee3ed  status=completed  verification=pending  node=local origin=stub
          outcome='incomplete'  result: 空
```

**`status` 说完成，`outcome` 说没完成，两者在同一个对象上。**
`verification=pending` 倒是诚实（没有证据）。

**但第 ④ 层其实是好的**——她没有被骗，而且每次都说清楚了：

```
[14:50:12] 没成。那个 test_ok.txt 小鱼去核对了，目录里根本没有文件，
           子 agent 说完成了但它是骗自己的。
[15:16:54] 那个子 agent 说的 hello-from-dsh.txt 居然真的在，不过是在它自己的
           agents 目录里，不在 workspace_probe。也就是说它没骗人，
           只是没写到说好的地方。
[16:20:13] 子 agent 今天跑过几单，可结果还挂着待验证，小鱼不敢算数。
```

**这是本次测试最重要的正面发现**：她不但读懂了，还自己发现了「文件在，但不在说好的地方」
这种最难的一种不实。

**但风险仍在**：如果某次她没有主动核对（`verification=pending` 时并无强制），
`status=completed` 会被下游（比如 `/mobile/tasks`、或她自己凭记忆复述）当成完成。

### 2.3 `dsh` 的失败回执不够可操作

```
status=failed  return_code=1  finish_reason=max-tokens  model=deepseek-flash
output: DSH execution failed; inspect runtime availability and model settings.
```

**她拿到的是一句通用英文**，没有说清是「运行时没起来」还是「模型截断」。
实测 `finish_reason=max-tokens`——真实原因是**输出被 token 上限截断**，不是运行时缺失，
但回执说的是 "inspect runtime availability"。

任务书已知 dsh 有问题、不要求修，这里只记录：**失败原因是错的**，会让她按错误方向排查。

### 2.4 `town` 的回执措辞是对的（值得保留）

```
status=completed  completion=accepted
output: 小镇交办状态：waiting。目标已保存，等待小镇决定回合处理；
        尚不能声称已经行动或玩完。
```

**这是今晚反复强调的那条原则的正例**：登记成功 ≠ 做成了，回执把两者分开了。

---

## 3. 边界条件实测（正常路径之外）

### 3.1 `command`：六种边界全部正确

| 场景 | 结果 |
|---|---|
| `echo ok` | completed, return_code=0, output=`ok` |
| `echo boom; exit 3` | **failed**, return_code=**3**, output=`boom` |
| 不存在的命令 | **failed**, return_code=**127**, output=`not found` |
| 5000 行输出 | completed，output 尾部 3 行，全量落 `output_path` |
| 中文 | completed, output=`中文` ✅ 无乱码 |
| 只写 stderr | completed, output=`oops`（合并了） |

**退出码非 0 正确判为 failed**，不是"completed 但有错"。

### 3.2 `work` 五个 op 的边界

| 场景 | 结果 |
|---|---|
| `write` 新建 / 覆盖 / 60000 字节 | 全部 completed，回执带准确 `bytes` + `sha` |
| `read` 不带参 / offset / offset+limit | completed，行号 `N|` 前缀正确 |
| `read` offset 超出末行 | completed + 空 output（**不报错**，合理） |
| `edit` 唯一命中 | completed，`found=1 replacements=1 sha=…` |
| `edit` **多处命中（不传 replace_all）** | **被拒**：`old 在 m.txt 命中 2 处；换更长锚点或传 replace_all=true` |
| `edit` 传 replace_all | completed，`found=2 replacements=2` |
| `edit` old 不存在 | **被拒**：`old 在 m.txt 里没有命中；先用 read 确认原文与缩进` |
| `grep` 基础 / include / 无命中 | 正确；无命中返回空 + `count=0`（**不是错误**） |
| `grep` 非法正则 | 被拒：`正则无效：unterminated character set at position 1` |
| `grep` 中文 | 返回空——**但需注意**：它不报错，只是没命中（本用例文件确实没有该中文） |
| `list` pattern / recursive | ✅ **确实生效**（见下） |
| 读 `/etc/passwd` | **被拒**：`路径超出工作区：/etc/passwd（工作区根 …）` |
| 写 `../escape.txt` | **被拒**，且解析成绝对路径后报告 |

`list` 的 `pattern` / `recursive` 我一开始误判成"被忽略"——**复测证明是我看错**：
`pattern=*.txt` 时目录项 `sub` 被滤掉、`sub/nested.txt` 被纳入；
`pattern=nomatch*` 返回 0 条；`recursive=True` 多出嵌套文件。**三个参数都真的生效。**

> 这条记下来是因为它差点变成一次误报：**幂等输出看起来像"参数没作用"，
> 实际是我没造出让参数产生差异的目录结构。**

### 3.3 `work` 的 `edit` 说明与实际**完全一致**

说明写「edit要求old在文件里唯一命中，否则先read确认原文与缩进再改」，
实测拒绝消息与这句一字不差地呼应。**这是说明与实现对齐的正例。**

---

## 4. 通过真实对话测的（不是直调 API）

| 我说什么 | 她实际做了什么 | 结论 |
|---|---|---|
| 「你现在手上能做哪些事？只说真能做到的，别报菜名」 | 回：「能用的是这些：云主机上跑命令、读写文件、把多步活一次交给后台代理做、上网搜和读网页、B站搜视频看字幕读评论、去小镇跟人互动」 | ✅ **不给工具名她也说得对**；且后面补了「没真用过的：上网搜、开网页…电脑是纯没连上」——**自己区分了「能做」和「做过」** |
| 「用 work 读 companion_work.py 231-240 行」 | `work op=read` 正确调用（但同时自己另派了 grep/command，属自主行为） | ✅ 明确点名时参数正确 |
| 「用 skill 列出做法库、读 computer 原文」 | `skill op=read arguments={"name":"computer"}` completed | ✅ |
| 「上网搜 deepseek v4 发布，只说几条、别打开」 | `web op=search arguments={"query":…,"num":8}` completed，返回真实标题 | ✅ 且**遵守了"别打开"** |
| （自主）读 index.html 300 行附近 | `work op=read offset=250 limit=130` | ✅ `offset/limit` 用法正确 |

---

## 5. 成本（套餐制，按要求只记账不省钱）

```
本时段（16:15 起）69 次调用
model = deepseek-v4.1-flash
input_tokens  = 2,513,594
output_tokens =     69,033
total         = 2,582,627
```
全天对比：`deepseek-flash` 576 次 / in 14,554,745 / out 364,158；
`deepseek-v4.1-flash` 94 次 / in 3,908,805 / out 91,983。
**模型名在 `companion_usage.model` 列里记录正确**（`usage` JSON 里没有模型名，别去 JSON 找）。

---

## 6. 无法验证的格子（不凑）

| 项 | 原因 |
|---|---|
| `computer` 的 ③④ | **台账 0 行**，历史上从未真正调用过。桌面 websocket 从未连入，`offline` 是如实状态。要验必须接一个真实桌面客户端，本次环境没有。 |
| `dsh` 的 ④ | 有回执，但回执给的原因是错的（说 runtime，实为 `max-tokens`），无法据此判断"她能不能理解"——**她理解到的会是错误的原因**。 |
| `body` 的 ③ | 38 次 completed 是历史记录（11-17 前后），今天桌面未连，无法复现成功路径。 |

---

## 7. 给下一个人

1. **`work.run` 要么实现、要么从说明里删掉**，并把第 238 行的错误消息改成不含 `run`。
   现在这句话是**自相矛盾**的，比单纯缺功能更坏。
2. **`agent` 的 `status=completed` + `outcome=incomplete` 需要一个统一口径**。
   她这次自己识破了，但那是她的警惕性，不是机制的保证。
3. **`dsh` 的失败回执别再写 "inspect runtime availability"**——实测是 `max-tokens`。
   错误的失败原因会把排查引向错误方向。
4. **本次差点误报 `list.pattern/recursive` 不生效**。教训：判断"参数有没有作用"之前，
   先造一个**能让参数产生不同输出**的输入；否则默认输出相同，看起来就是"参数无效"。
