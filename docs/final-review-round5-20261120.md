# 终局评审第五轮：前四轮 blocker 复核 + 最高原则验收（P79 / t46）

评审者：reviewer（独立评审，不改代码、不重启服务）
取证时刻：**2026-11-20 08:44 CST**，`git rev-parse --short HEAD` = **`b3cf49d`**，
`systemctl is-active companion.service` = **active**（ExecMainStartTimestamp `2026-11-20 02:04:30 CST`，MainPID 2377659）

最高原则：
> **她可以查也可以不查，根据她自己来判断。我们将世界所有事实摆在她的眼前，她来进行所有的判断。**
> 判据：① 她能不能查到；② 查到的对不对。不问：她该不该查 / 她查了没有 / 她说得好不好。

**取证方法**：只读。`git`、生产 `life.db` 只读副本（`mode=ro`）、在云端真源码上跑真模块（`failure_summary` /
`_usage_summary` / `_provider_choices` 对真实库实跑）、`curl /alive` 打点。**未改代码、未重启服务、未写库。**

---

## 终局结论

```
PASS_WITH_NOTES
```

**能收口。** 前四轮全部 blocker 里，**决定性的一条（t31 G-1）真修了**，其余 blocker 均已修或在影响面内为零。
剩下的是 §4 的 7 条已知限制，其中 **§4-5（`seconds` 报 0.1 秒而日志有 7 KB）是"查到的数字在撒谎"**，
建议在收口前修掉——改动面是一行 `with` 的作用域。

**为什么不 REJECT**：剩下没有一条让她"看不到自己的核心事实"——重启史与 git 入口都能用 `command` 绕过，
取消原因只影响历史行，`seconds` 只影响一个数字字段。
**为什么不给纯 PASS**：按用户那句原则，"查到的对不对"是硬线，§4-5 越过了这条线。

---

## 1. 前四轮 blocker 复核（逐条给证据与验证命令）

### t7（第一轮，15 条 finding，3 blocker）

| ID | 内容 | 现在 | 证据 / 验证命令 |
|---|---|---|---|
| R-01 | `AGENT_INSTRUCTIONS` 教 `inspect{agent_id}` 顶层写法 | ✅ **修好** | `python -c "import life.companion as C; print(C.AGENT_INSTRUCTIONS)"` → 「inspect 看状态与验证，**arguments 写 `{"agent_id":"…"}`**」；7 条示例逐条过 `OP_FIELDS` 零多余键 |
| R-02 | 项目说明与部署版本不同步 | ⚠️ **仍不完全** | `ls docs/project-self-description.md` → **无此文件**；`git remote -v` → **无输出**（云端仓库无 origin）。云端现在是"唯一版本"，`git log` 可查历史，但**没有回推上游的通道** |
| R-03 | watchdog `what_it_does` 与 inactive 状态矛盾 | ✅ **修好** | `companion_awareness.py:808/815/825`：三档分别给「重启后…」(active)、「**现在没有在做事**：这个守护没在运行（%s）」(inactive)、「我读不到它的状态，所以不能确认…」(unknown) |

### t24（第二轮，7 blocker）

| ID | 内容 | 现在 | 证据 |
|---|---|---|---|
| B-1 | `ask` 承诺 `current_state.questions` | ✅ | `companion.py` 产出该键；读不到写 `{'error': …}` 不写 `[]` |
| B-2 | 第 4 组"其他修复"没做 | ✅ | `_operation_status` / `_verify` / `backfill_unverified` 全在 |
| B-3 | 通道条目判定与措辞两份源 | ✅ | `companion_awareness.py:983/1003`：`detail_by_state(state_) or channel_reason(status_)` |
| B-4 | `task clear` 说隐藏没隐藏 | ✅ | `companion_tasks.py:217/262`：真写 `hidden_at`，`list()` 默认过滤 |
| B-5 | md5 表过期 | ⚠️ | 见 t31 G-7（锚点仍缺） |
| B-6 | `provider_tools` 旧注释 | ✅ | `grep -c provider_tools life/providers.py` → **0** |
| B-7 | 腾讯新地址认不出 | ✅ | `providers.py:160` 有 `tokenhub.tencentmaas.com`；`_self_check()` 在 import 时跑，负向测试会 `AssertionError` |

### t29（第三轮，四类错误清单）

| ID | 内容 | 现在 | 证据 |
|---|---|---|---|
| F-1 / F-17 | `verification=pending` 假终态 | ✅ | 台账 `SELECT status, verification, COUNT(*) FROM companion_agent_tasks GROUP BY 1,2` → **7 行全 `unverified`**（completed 5 / failed 1 / interrupted 1，0 条 pending） |
| F-2 | `inspect{agent_id}` | ✅ | 同 t7 R-01 |
| F-5 / F-6 | dsh 错误消息给处方、把 401 说成"别家的 key" | ✅ **修得比要求的更彻底** | `companion_dsh_worker.py classify_failure`：docstring 明写「**只陈述事实，不给处方**」；401 分支给「鉴权被拒（HTTP 401）。底层原文见下面的 reason 行。当前配置：provider=%s、base_url=%s。dsh 的 adapter 是 dsh-llm-deepseek-api-key，它只读环境变量 DEEPSEEK_API_KEY、不读 base_url。… **以上是这条错误和当前配置里的事实。**」——**给了因果（adapter 只读环境变量），没给"得换 provider"这种处方** |
| F-7 / F-8 | 命令式措辞 | ✅ | GUIDES_BRIEF 相关句已改陈述 |
| F-9 / F-10 | 后台无 `op=list` | ✅ | `companion_background.py:83` `OPS=(…,'list')`；实跑返回 `count=2` 带 job_id/state/exit_code |
| F-13 | 排队超时落成 `failed` 但正文说"不代表失败" | ⚠️ **仍在** | 见 §4-4 |
| F-18 / F-19 | 截断文案把"不知道"说成自责 | ⚠️ 部分 | 排队→见 §4 |
| F-21 | 重启史无入口 | ❌ **仍未做** | 见 §4-1 |
| F-22 | git 对她是种"存在但没摆在眼前" | ❌ **仍未做** | 见 §4-2 |

### t31（第四轮，§1 八个 + §4 五个）

| ID | 内容 | 现在 |
|---|---|---|
| §1 B-1~B-7 | 七个 blocker | 全部同 t24 结论（6 修好、B-5 半修） |
| §4 **G-1** | 后台 `op=list` 做了但说明里没有 | ✅ **修好了 —— 这是本轮决定性的一条** |
| §4 G-2 | `failure_summary` 把同一种"当时没记"拆成两类 | ✅ **修好** |
| §4 G-3 | §0.13 改写脚本不可审计 | ⚠️ 见 §4-3 |
| §4 G-4 | 重启史 / token 用量 / 服务商可选 / 22 条 cancelled 无原因 | 3 条修好、1 条未做（见 §2） |
| §4 G-5 | `background.list` 的 `seconds` 恒 None | ⚠️ 见 §4-5 |

---

## 2. t31 §4「本轮新发现 blocker」逐条核（任务书点名要核）

### G-1 后台 `op=list` 不在任何说明里 —— ✅ **修好了**

```bash
grep -c 'op=list' life/companion_context.py     # → 5
python -c "from life import companion_context as c; print('list' in c.GUIDES_BRIEF['command'])"  # → True
```

原文（BRIEF 行）：
> `command: 跑一条 shell 命令（写 command）。长命令用 op=start 后台跑，回执给 job_id；`**`op=list 列出全部后台任务（给 job_id、状态、启动时间、已跑多久、日志路径）`**`，再用 op=status/logs/wait/stop 查和停。`

**这是本轮最该表扬的一处**：不但在详细版加了，**简易版也加了**——她生活模式下就知道能枚举。

### G-2 `failure_summary` 把「回填写于修复之前」和「这条没记原因」当成两件事 —— ✅ **修好了**

```bash
sed -n '119,124p' life/companion_awareness.py
#   「回填写于修复之前，原因当时没记」是「这条没记原因」的一种，
#       key = '（这条没记原因）'
```

**对生产实跑验证**：`failure_summary(Core())` 24h → `total=38`，8 类，**没有出现重复的"没记原因"桶**；
168h → `total=400`，8 类，`（这条没记原因）` 合并成一桶（289 条回填 + 35 条真空白）。
这正是 §0.12 想说的那件事的正面写法。

### G-4 四条「存在但查不到」

| 条目 | 现在 | 证据 |
|---|---|---|
| 后台任务列表 | ✅ | `op=list` 实跑 `count=2` |
| 今日错误汇总 | ✅ | `failure_summary` 24h `total=38`、8 类，且 `how_to_look` 给入口 |
| 子 agent 验证结论 | ✅ | 台账 7/7 全 `unverified`；`_summary` 标「子 agent 自己的说法（不是系统判定）」 |
| **本轮 token 用量** | ✅ **本轮新修** | `companion_awareness.py:312 _usage_summary` + `:1320` `usage=run('usage.summary', …)`。对生产实跑：`rounds=784, request_tokens_total=19163204, request_tokens_max=53748, last_round_tokens=17635, last_round_at=2026-11-20T08:28:31+08:00, model_calls=959` |
| **服务商可选** | ✅ **本轮新修** | `companion_awareness.py:348 _provider_choices` + `:1321` `providers=run('providers.choices', …)`。实跑：`current=opencode-go / deepseek-v4.1-flash`，`available=7` 家，每家带 `supports_tools / needs_manual_model / models[:6]` |
| **重启史** | ❌ **仍未做** | 见 §4-1 |

**t31 那 6 条 ❌ 的现状：5 条修好 / 1 条（重启史）仍是 0。**

### G-5 `background.list` 的 `seconds` 口径 —— ⚠️ **代码改了，但生产上 21/22 条任务报 `seconds=0.0`，且多数是真值**

上一轮我判"`list()` 的 `seconds` 恒 None"。现在代码里有 `elapsed_seconds` 三档计算
（`companion_background.py:184-214`：在跑→实时算；有 `seconds`→用它；否则 `end - started`，`end` 优先取 `finished`、
其次取**日志文件 mtime**）。

**但对生产实跑发现问题**：`BackgroundJobs('/var/lib/companion/xiaoyu').list()` 返回 22 条里
**21 条 `state='gone'`**，其中 **18 条 `seconds < 1`**。我逐条对账后发现**不是全错**：

```bash
python - <<'PY'
import json, os
raw = json.load(open('/var/lib/companion/xiaoyu/data/companion/background/jobs.json'))
for jid, r in raw.items():
    st = float(r['started']); mt = os.path.getmtime(r['log'])
    print(jid, round(mt-st,1), os.path.getsize(r['log']))
PY
```

| job | 日志字节 | 真实耗时（mtime−started） | list 显示 | 判断 |
|---|---|---|---|---|
| `bg_909eeb3b4c2d` | **7265** | **0.1s** | 0.1s | ❌ **不可能**：写了 7 KB 只要 0.1 秒 |
| `bg_6ef2c1541e6e` | 1155 | 0.0s | 0.0s | ❌ 不可能 |
| `bg_abf15beacf8d` | 3317 | 0.0s | 0.0s | ❌ 不可能 |
| `bg_361cdd8136a2` | 4336 | 0.0s | 0.0s | ❌ 不可能 |
| `bg_ad06a7e8e7d8` | 18 | **20.0s** | 20.0s | ✅ 对（`sleep 20`） |
| `bg_3846a2b7e996` | 7 | **15.0s** | 15.0s | ✅ 对（`sleep 15`） |

**根因**（`companion_background.py` `start()`）：日志文件用 `with log_path.open('wb') as sink:` 传进
`ProcessTree(..., stdout_file=sink)`，**`with` 块在父进程里立刻关闭了自己的句柄**。
子进程拿到的是它自己 dup 出来的 fd，写日志会推进子进程那份的写入，
但**文件的 mtime 只在父进程的句柄关闭那一刻更新过一次**——于是 `mtime ≈ started`，
`seconds` 算出来永远是 0 附近。真实走了 20 秒的那两条对得上，是因为它们被 `_exit_code` 的 `poll()` 分支
用别的口径补过。

**影响面**：`state` 判定（`gone`）不受影响；受影响的是**"这个任务跑了多久"这个数字**。
按最高原则，这是**"查到的和事实不符"**——不是"查不到"。

---

## 3. 最高原则验收：她能不能查到、查到的对不对

**只问这两条，不问别的。** 每格给可复现命令。

### 3.1 「事实存在」且「她查得到」—— 逐条对生产实跑

| 事实 | 她的入口 | 实跑结果 | 对不对 |
|---|---|---|---|
| 我用的服务商/模型 + 我还能选哪家 | `memory op=awareness` → `providers` | `current=opencode-go/deepseek-v4.1-flash`，`available=7` | ✅ 与 `config.toml` 一致 |
| 本轮/近 24h 吃了多少 token | `memory op=awareness` → `usage` | `last_round_tokens=17635`，`rounds=784`，`max=53748` | ✅ 取自 `companion_context_loads` 真实行 |
| 最近失败有多少、哪几类 | `memory op=awareness` → `failures` | 24h `total=38`，8 类；入口指 `memory op=search scope='errors'` | ✅ 与台账 `GROUP BY` 一致 |
| 失败记录的原文 | `memory op=search scope='errors'` | 逐条给 `at/text/reason`，没有 reason 时明写「这条记录没有写原因（当时没记）」 | ✅ |
| 后台任务有哪些 | `command op=list`，**BRIEF 也有** | `count=2` | ✅ |
| 单个后台任务生死 | `op=status/logs/wait/stop` | `state=running/finished/failed/**gone**` | ✅ `gone` 是"不知道成没成功"，正例 |
| 挂着的提问与它的回答 | `current_state.questions` | 带 id/question/status/answer/answered_at/late | ✅ |
| 我的待办与"做过几次" | `task op=list` | 带 `runs`、`stuck_for`、`blocked_on` | ✅ |
| 子 agent 跑成什么样 | `agent op=inspect` + 摘要 | 「子 agent 自己的说法（不是系统判定）」分开标 | ✅ |
| 我的能力与不可用原因 | `memory op=capabilities` | 每条带 reason/evidence/checked_at/what | ✅ |
| 能力状态 + 用法 | 同上 | `what` 从 GUIDES 切出 | ✅ |
| 各连接状态 | 每轮 `current_state` | phone/computer/body/qq/wechat/town/bilibili 全在 | ✅ |

### 3.2 「查到的对不对」—— 我做了两次交叉核对

**（a）`usage.last_round_tokens` vs 台账**
```bash
python -c "…_usage_summary(Core())…"   # last_round_tokens=17635, at=2026-11-20T08:28:31
sqlite3 'file:life.db?mode=ro' "SELECT request_tokens,datetime(at,'unixepoch','+8 hours') FROM companion_context_loads ORDER BY at DESC LIMIT 1"
```
→ 同一行、同一值。**她看到的数字就是库里那一行。**

**（b）`failures.total` vs 台账**
```bash
python -c "…failure_summary(Core())..."          # total=38
# 台账：SELECT COUNT(*) FROM companion_activity WHERE type='error' AND created >= now-86400
```
→ 一致。**不是"看起来像"的汇总，是能一键查出由哪几行算出来的。**

### 3.3 「事实存在，但她查不到」或「查到的和事实不符」

见 §4 的 4-1/4-2/4-6（查不到）与 4-5（查到的数字是错的）。

---

## 4. 已知限制与缺陷（写清，供收口后跟进）

### 4-1 重启史仍无入口（t29 F-21 / t31 G-4 未做）

```bash
python -c "import inspect, life.companion as C; src=inspect.getsource(C.Companion._self_process_facts); print('started_at' in src, 'restart' in src)"
# → False False
sqlite3 'file:life.db?mode=ro' "SELECT k FROM meta WHERE k LIKE '%restart%' OR k LIKE '%boot%' OR k LIKE '%uptime%'"   # → 空
```
**她查不到「我上次为什么重启」。** 她能 `command` 跑 `systemctl show companion -p ExecMainStartTimestamp`（能拿到时间），
但**"为什么"（部署 / 崩溃 / 人手动）没有任何一方记录**。
**最小补法**：启动时把「启动原因 + 时间 + 上一版 HEAD」写进 `meta` 一行，`self_process` 下发。

### 4-2 git 是"存在但没摆到眼前"（t29 F-22 未做）

```bash
python -c "import inspect, life.companion as C; print('git' in inspect.getsource(C.Companion._self_process_facts))"  # → False
```
云端 **`/opt/qq-deepseek/.git` 存在，`git log` 有今天全部提交**，她也**能**跑
`command: cd /opt/qq-deepseek && git log`（`command` 默认 cwd 是家目录，需自己拼路径）。
但 `self_process` 只给 `source_root`，`GUIDES` 只字不提版本控制。
**"她能不能查我自己改了哪些文件"** → 能；**"这件事有没有摆在她眼前"** → 没有。
**最小补法**：`self_process` 加一句 `version_control=dict(kind='git', root=<source_root>, hint='git log --oneline -20 / git show <sha>')`。

### 4-3 §0.13「云端改文件的唯一写法」仍不可审计（t31 G-3）

改写脚本不进仓库。**可判的间接证据**：今晚两次整文件重写后符号数没减少（`agents 28→28`、`awareness 58→59`），
`companion_dsh_worker.py` 的 `__main__` / `_turn_error` / `classify_failure` 全在（201 行）——
**没有复现 §0.13 记的那次事故**，但"每次改写都做对了"这件事本身仍靠人自觉。
**最小补法**：每次改写把脚本与"改前/改后符号计数"粘进对应报告（§0.13d 已要求，只差执行）。

### 4-4 排队超时落成 `failed`，正文说"不代表失败"（t29 F-13）

```bash
grep -n "queued_timeout" life/companion.py
# 落库 status='failed'，reason 里写「…这不代表它做完了，也不代表它失败了——是它根本没开始」
```
台账词与正文不一致。**她读 `status` 会读成"失败"。**
**最小补法**：用一个独立状态（`never_started` / `abandoned`），或至少在 status 旁给 machine-readable 的 `failure_kind`。

### 4-5 `background.list` 的 `seconds` 算出 0，与真实耗时不符（t31 G-5，**新发现的具体缺陷**）

见 §2 G-5。`start()` 用 `with log_path.open('wb') as sink:` 把句柄交给子进程后**父进程句柄立刻关闭**，
文件的 mtime 冻结在 `started` 那一刻；`list()` 的第三个分支拿 `log mtime - started` 当耗时，
于是"跑了多久"恒为 0。生产 22 条里 18 条 `seconds < 1`，其中 `bg_909eeb3b4c2d` 写了 **7265 字节**却报 **0.1 秒**。

```bash
python -c "from life.companion_background import BackgroundJobs; print(BackgroundJobs('/var/lib/companion/xiaoyu').list()['jobs'][:3])"
# 每条看 seconds vs log 字节数：bytes 大而 seconds≈0 的就是错的
```

**最小补法**：`start()` 保留 `sink` 句柄不关闭（交给 `ProcessTree` 管理，关闭放到 `reap`/结束时），
或在 `_exit_code()` 拿到 `poll()` 结果时把 `finished=time.time()` 落盘并让 `list` 优先用它。

### 4-6 23 条 `cancelled` 行动仍无原因（t31 G-4 第四项）

```bash
sqlite3 'file:life.db?mode=ro' "SELECT COUNT(*) FROM companion_actions WHERE status='cancelled'"
# → 23（全部 23 条都缺 reason）
```
§0.11 第三例，文档自己标了「尚未处理」。**她查到这些只会知道"取消了"，不知道为什么。**

### 4-7 md5 报告仍无锚点（t24 B-5 / t31 G-7）

`docs/p44-blocker-fixup-20261118.md` 的 6 条 md5 里 2 条已过期（`companion.py`、`companion_awareness.py`）。
**最小补法**：报告头部带 `git rev-parse --short HEAD`，让读者自己能判断是否适用。

---

## 5. 收口判断

```
PASS_WITH_NOTES
```

**为什么不是 REJECT**

1. **t31 G-1（上一轮唯一的新 blocker）真修了**，而且修到了简易版——不是补在只有工具模式才看得见的地方。
   `grep -c 'op=list' life/companion_context.py` → 5。
2. **G-2 真修了**，对生产实跑 `failure_summary` 不再把同一种"没记"拆成两类。
3. **我上一轮列的三条 ❌ 里，token 用量与服务商可选都补齐了**，并且对生产实跑有真实数字。
4. **"查得到且对"这条硬线，12 个入口逐个实跑通过**，其中两条做了与台账原始行的交叉核对。
5. **剩下的问题没有一条是"她看不到自己的核心事实"**：重启史与 git 入口都能用 `command` 绕过，
   取消原因只影响历史行，`seconds` 只影响一个数字字段。

**为什么不是纯 PASS**

按用户那句原则，"查到的对不对"是硬线。§4-5 的 `seconds` 是**查得到的数字在撒谎**（7265 字节报 0.1 秒），
属于"路指错了"，不是"少一条路"。它不是架构问题，但它正是这一整轮反复出现的同一类错误
（§0.12：把两样东西当成同一个；这里是把"文件被创建的时刻"当成了"任务结束的时刻"）。

**所以：能收口，但 §4-5 应在收口前修掉**——它是一行 `with` 的作用域问题，改动面极小，收益是她以后问
"那个任务跑了多久"时拿到的是真数。

**收口后建议立刻补的五条（按性价比排序）**

| 优先级 | 事项 | 为什么排这个位置 |
|---|---|---|
| 1 | 修 `start()` 的日志句柄，让 `seconds` 报真实耗时（§4-5） | 一行作用域问题；不修就是"查到的数字在撒谎"，违背硬线 |
| 2 | `self_process` 加 `version_control`（§4-2） | 一句话的事，但直接决定"她能不能查自己改了什么" |
| 3 | 启动原因 + 时间写进 `meta` 并在 `self_process` 下发（§4-1） | 今天服务至少重启两次，她目前完全不知道 |
| 4 | 排队超时改用独立状态（§4-4） | `failed` 这个词会让她的判断偏掉 |
| 5 | 23 条 cancelled 带审计回填原因（§4-6） | §0.11 第三例，一件一次性的事 |

---

## 6. 取证清单（可复现）

```bash
date                                              # Fri Nov 20 08:44:16 AM CST 2026
git -C /opt/qq-deepseek rev-parse --short HEAD    # b3cf49d
systemctl is-active companion.service             # active
systemctl show companion -p ExecMainStartTimestamp --value   # Fri 2026-11-20 02:04:30 CST

grep -c 'op=list' life/companion_context.py                        # 5
grep -c provider_tools life/providers.py                           # 0
sed -n '119,124p' life/companion_awareness.py                      # G-2 合并逻辑
sed -n '312,346p' life/companion_awareness.py                      # _usage_summary
sed -n '348,378p' life/companion_awareness.py                      # _provider_choices
grep -n "usage=run\|providers=run\|failures=run" life/companion_awareness.py   # 1320/1321/…

sqlite3 'file:/var/lib/companion/xiaoyu/data/life.db?mode=ro' \
  "SELECT status, verification, COUNT(*) FROM companion_agent_tasks GROUP BY 1,2"   # 7/7 unverified
```

**我没做**：不改代码、不重启服务、不写库、不对模型服务商发真实请求、
没有复跑 t35/t37 的 CPU 压测（只做了 `/alive` 打点：5 次 1.3–1.7ms）、
没有验证 chunker v3 / 索引 44092 行（属 t44/t45/索引线，不在本次 blocker 范围）。
