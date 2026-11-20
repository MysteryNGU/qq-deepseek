
> **把「不知道」画成「坏了」，是把不确定性变成假消息。**

> **示例代码不是可执行规格，它本身要过一遍。**

---

## §0【最前置】"系统现在有什么"的断言，先对齐三份

今晚同一件事被三个 agent 分别读成了三种结论，根源都在这里。**`companion.py` 今晚被四方改过，分叉是常态不是例外。**

| 位置 | 含义 | 怎么取 |
|---|---|---|
| **本地工作区** | 某个 agent 正在写、可能未提交 | `md5sum` 直接读 |
| **本地 HEAD** | 已提交、未推送 | `git show HEAD:life/companion.py \| md5sum` |
| **云端生产** | 真正在跑的那份 | `ssh ... md5sum /opt/qq-deepseek/life/companion.py` |

### 规矩
1. **任何"某段代码已实现"的断言**，必须写清查的是哪一份，并附 `md5sum` + `wc -l`。
2. 三份不相等时，**基于代码的结论只对其中一份成立**——不能默认"本地的就是生产的"。
3. 行号必须用 `grep -n` 现取，**不能沿用别处的行号**（行数不同会整体错位）。
4. 带行号的文档，过一遍 `tools/verify-doc-line-refs.py`。

### 实例（2026-11-18）
- 云端 `837513ab…` / 3339 行 → 无插话机制
- 本地工作区 `e0c5f8c4…` / 3220 行 → 有 63 行插话代码（t13 正在写）
- 某人只读了本地，报"需求已实现"，队长据此差点以为派错了任务

### 贯穿全篇的一类 bug：**返回一个看起来合理的错误答案**

它不报错，它给出一个语法正确、语义合理、**但不是事实**的值。今晚反复出现：

| 现象 | 看起来像 | 实际是 |
|---|---|---|
| `channel.qq` 报 `ready` | 「我能用 QQ」 | 只看配置，不管账号登没登 |
| `town` 报「没启用」 | 配置没开 | 读的是初值，没问 `TownConfig` |
| `phone` 报「没连上」 | 手机没连 | 根本没探测，拿不到状态 |
| 查错 `agent` 返回 `found: False` | 「引用解析坏了」 | 主键写错，静默查空 |
| `all()` 在空数组上返回 `True` | 「全部通过」 | 一条都没验到 |

**共同点：错误被包装成了结论。** 报错会被看见，这个不会。

所以凡是「我没拿到状态」的地方，**必须返回 `unknown` 并说明拿不到**，
不许退化成 `offline` / `needs_setup` / `False` 这些**看起来像判断**的值。


## §0.13 用截断式改写代码后，必须验证「文件完整性」

**症状**：`import` 成功、`py_compile` 成功、AST 成功，但**功能消失**。

**为什么静态检查抓不住**：
import 只检查「能不能加载」，不检查「该有的东西在不在」。
删掉 `if __name__ == '__main__':` 之后，模块依然是一个合法可导入的 Python 文件。

**2026-11-18 实例**：
```python
src = src[:idx] + new_block      # ← 把 idx 之后的所有内容全删了
```
本意是替换 `classify_failure` 一个函数，实际把 `_turn_error()` 和整个 `__main__` 入口块一起丢掉。
直接后果：

```
worker 被调用 → 什么都不做 → exit 0
     ↓
status=completed, return_code=0, output='', seconds=0.066
     ↓
她每次调 dsh 都得到一个"成功"，而实际什么都没做
```

**而 `import life.companion_dsh_worker` 是成功的。**

**改完之后立刻验三样**：
1. 顶层符号还在不在（`grep -c "__main__"`、关键函数名、`grep -c "def xxx"`）
2. 总行数与改前比较（大幅减少 = 有东西丢了）
3. **每个被删/被替换区域的前后各一行**，确认边界正确

**再加一条**：
> **凡是「必须有产出」的工具，静态检查通过之后，必须真跑一次，验证产出非空。**

---

## §0.13c 云端改文件的唯一写法

**本仓库的云端文件在服务器上，本地 `edit` 工具够不到，所以每次都是「写脚本 → 执行」。**
以下写法是唯一允许的：

```python
old = """精确的多行原文"""
new = """精确的多行新文"""

assert src.count(old) == 1, '命中 %d 次，不唯一' % src.count(old)   # ← 防线
src = src.replace(old, new, 1)
```

**禁止**：
- `src[:idx] + new`（边界算错就静默丢内容）
- `src.split(...)[0] + ...`
- 正则替换

**理由**：`count == 1` 把"锚点写错"和"锚点不唯一"变成立刻报错；
截断式写法把同样的错误变成**静默删掉文件的一大段**。

---

## §0.13d 截断式改写后必须复核符号清单

**`ast.parse` 抓不住"语法合法的删除"。**
删掉 `if __name__ == '__main__':` 之后，模块依然是合法的 Python 文件，`import` 依然成功。

**必须做的复核（改完之后立刻）**：

```bash
# 改前
grep -cE "^(def |class |if __name__|async def )" 文件   → N
# 改后
grep -cE "^(def |class |if __name__|async def )" 文件   → M
# M < N 且不是故意删 → 报错
wc -l 文件    # 总行数大幅减少也要报
```

**更稳的做法**：改前把顶层符号列表存下来，改后逐个比对。

**2026-11-18 实例**：`src[:idx] + new_block` 把 `classify_failure` 之后全丢了，
包括 `_turn_error()` 和整个 `__main__` 入口块。
结果 worker 变空壳、exit 0、`status=completed`、`output=''`，
**而 `import` 是成功的。**

---

## §0.21 修「积压不处理」类 bug 之前，先查积压量

**2026-11-19 夜，两次踩同一个坑：**

```
第一次（P69）：修好索引扫描空转退避 → CPU 102.9% → 9.4%
第二次（P70）：修好 claim 的闸门（index_access_fence IS NULL）
              → 1026 条积压同时可领
              → claim_next 的重 SQL（json_extract + NOT EXISTS）每轮都跑
              → 领到就 build，打到本地 embedding 服务（并发 2 路就 HTTPError）
              → CPU 从 14% 涨到 79.8%
```

**修任何「卡住」的东西之前，先问三句：**

```
① 卡了多久？（due 过期 251899 秒 ≈ 70 小时）
② 积压了多少条？（1007 条 unknown）
③ 放开之后下游扛得住吗？（8231 安全并发 = 1）
```

**顺序颠倒的代价：改动本身是对的，但上线后要回滚。**

**判据：凡是"让一批原本不流动的东西开始流动"的改动，
必须先给出「每轮最多处理 N 条」的限流，再放开闸门。**

---

## §0.20 不要在她正在核对的证据上做任何改动

**测试可以造数据，但：**

```
造数据 → 在她开始核对之前完成
清理   → 在她核对结束并确认之后
她正在看的时候 → 数据必须静止
```

**2026-11-18 实例**：
```
23:29:38  capa 通过她的 op=start 起了 sleep 40 的测试 job
23:30:52  她念出 "job_id=bg_c1fba6c7a451, pid=1878999, 日志落在 …"   ← 正确
23:31 前后 capa 清理测试数据，删掉了那个 job
23:32:50  她再核 jobs.json → 找不到 → 认错「一个都对不上」           ← 她没错
```

**这不是「清理失误」，是「在被观察者核对的过程中改动被观察对象」。**
**她不但没错，还主动认了个不该认的错。**

**这条和 §0.19 不是一类：§0.19 是技术性错误（属主），这条是改变了她判断所依据的世界。**

**判据：如果你在「造/删测试数据」，先问一句「她现在是不是正看着这份数据」。**

---

## §0.19 权限：一开始就用正确身份，不要事后修

**用 root 写 `/var/lib/companion/**` 或 `/opt/qq-deepseek/**` 之后必须 `chown`——
这个「必须」本身就是错的：它把「先做错再补救」写成了流程。**

**正确的做法：写之前就问「为什么不能用 companion 身份」。**

```
改文件内容           → sudo -u companion python …
改 systemd unit      → 才需要 root
改 /etc              → 才需要 root
装包                 → 才需要 root
```

**今天的两次实例：**
```
jobs.json chmod 600 后属主变 root:root，服务以 companion 运行 → 读不到
  → op=list 返回 count=0「还没有后台任务」，而文件里明明有 4 条
  → PermissionError 被吞掉返回空，看起来像"没有数据"
```

**判据：发现自己在 root 下碰用户数据目录时，停下，问一句「为什么不用 companion」。**

---

## §0.18 查「某段时间发生了什么」之前，先确认口径存在

**三步，顺序不能颠倒：**

```sql
-- 1) 确认你要查的 kind 真的存在
SELECT kind, COUNT(*) n FROM events GROUP BY kind ORDER BY n DESC;

-- 2) 用真实存在的一个样本，确认时间字段和时间范围对得上
SELECT received_at, kind FROM events
 WHERE kind='<真实存在的kind>' ORDER BY received_at DESC LIMIT 3;

-- 3) 才开始查内容
SELECT ... WHERE received_at > :anchor AND kind='<确认过的kind>';
```

**理由：2026-11-18 一个晚上两个人各错一次，都是因为「查 0 条」被当成了「世界是空的」。**

```
anchor 常量手写错（1795015762 实际是 1795014562，差 1200 秒）
  → 查得 0 条 → 断言「她没有下一轮」→ 实际有 4 条

查了一个不存在的 kind（self_output，全库 0 条）
  → 断言「她没念」→ 实际她连念了 6 条 message
```

**「查不到」有两种含义：真的没有，或者你查的口径不对。**
**在确认口径之前，不许说「没有」。**

**这条对人和 agent 同样适用。转述别人的观察时，也要先确认对方说的那个名字
在你这边存在——`self_output` 这个错就是从别人的转述里带过来的。**

---

### §0.18 补充：epoch 一律现算，不手写

**今天的实例**：手算 `1795018179` 当成 23:29:39，
实际是 `2026-11-19 00:09:39`（差 40 分钟），导致「之后没有下一轮」这个错误结论。
正确的是 `1795015779`，用 `datetime(2026,11,18,23,29,39).timestamp()` 现算得出。

```python
# 对：现算
anchor = datetime.datetime(2026, 11, 18, 23, 29, 39).timestamp()

# 错：手写常量
anchor = 1795018179   # 哪个时间？没人知道
```

**判据**：
- 任何时间点 → `datetime(...).timestamp()` 现算
- 任何时间窗 → **先跑一条已知样本证明查询能命中它**，再说「没有」

**更根本的：手写常量 = 用一个没有来源的数字做判断，和「方法名靠猜」是同一类。**

---


## §0.16 报告里不写 md5 静态表，写取证命令

**2026-11-18 第二轮评审 B-5**：一轮报告里列了 6 个文件的 md5，评审时 2 条已经对不上。

**这不是"要更勤地更新"，是"表格里的 md5 必然过期"**——
写报告的那一刻它是对的，被读的那一刻它大概率已经错了。

**正确写法**：

```bash
# 报告里写这条命令 + 取数时刻，而不是写 md5 值
date; md5sum life/companion.py life/companion_agents.py \
       life/companion_awareness.py life/companion_context.py \
       life/companion_background.py life/companion_executor.py; \
       git -C /opt/qq-deepseek rev-parse --short HEAD
```

**判据**：任何人跑这条命令，拿到的是"现在的"，不是"写报告时的"。
**报告里可以写「我改的是这些文件」，但不要写「它们的 md5 是 …」。**

**同理适用于**：文件行数、commit 列表、测试通过数——凡是在变的，给命令不给快照。

---

## §0.17 改写脚本要进仓库

**2026-11-18 第二轮评审 §0.13 不可审计**：
用于改代码的 Python 脚本写在 `/tmp`、跑完就没了，只能靠结果反推"当时改了什么"。

**规矩**：凡是用脚本改仓库里的代码，脚本存到 `ops/patches/` 下并进 git。

```
ops/patches/
  20261118-2306-03-awareness-three-facts.py   # 文件名含时间戳 + 目标
```

脚本头注释必须写：

```python
"""改了什么、为什么改、改前取证是什么。
目标文件: life/companion_awareness.py
改前 md5 / HEAD: <值>
"""
```

**判据**：任何一个改动，都能查到是哪个脚本改的、改前状态是什么。

---

### §0.17 补充：匹配 JSON 内容用 json_extract，不要用 LIKE

```sql
-- 错：格式一变就落空
SELECT ... WHERE action LIKE '%"op": "list"%'

-- 对：按字段值筛
SELECT ... WHERE json_extract(action, '$.op') = 'list'
```

**今天的实例**：`action` 里存的是 `"op":"list"`（无空格），
我用 `%"op": "list"%` 匹配不到，于是误判「查不到」——第 10 次口径问题。

**判据：凡是要从 JSON 字段里筛，第一反应必须是 `json_extract`，不是 LIKE。**

**为什么不做成脚本**：加一个 `query_like.sh` 下次还是会忘掉去用它；
而「不许用 LIKE 匹配 JSON」这条规则本身就能生效。
**忘不忘是意志问题，用对工具是方法问题。**

---


## §0.15 提到方法名之前，先 grep

**写 `self._xxx()` 或 `foo.bar()` 之前，先 `grep -n "def xxx" 文件`。**
不确定就先 grep，不要「这种类应该有这个方法」。

**2026-11-18 六个实例，全部一 grep 就能避免：**

```
队长的任务书：
  finish(active, ...)          实际 finish 是 String，且只做 id.equals(active)
  _state('work_mode') is 'tool' 字符串比较必须用 ==
  SETTLED_OK 的语义              实际是「有回执」的集合，不是「已完成」

capability-engineer 的方法名：
  self._note(...)              实际叫 _log
  Agents._verify               实际在 AgentManager
  self._state_of(...)          实际是 _row / _alive / _exit_code
```

**共同点：推理冒充事实。** 而每一次都只要一条 grep。

**这条对人和 agent 同样适用**：
```
能跑就不要猜。
不能跑的时候，要把「这是推演」写进指令本身。
```
**责任不对称要认清**：有环境的一方该真跑；没有环境的一方该把语气降级，
写明「这是推演，你去验」——而不是用肯定语气写出来，让对方误以为是既定事实。

---

## §0.14 校验通过 ≠ 可用

**schema 校验只证明「结构合法」，不证明「照着做能成」。**

凡是「说明文档 / 示例 / 指引」类的东西，判据必须是：

> **真的照它做一遍，看会不会失败。**

**2026-11-18 实例**：
`AGENT_INSTRUCTIONS` 里写 `inspect{agent_id}`，
拿 24 条示例「真构造 action 过 schema」——**全部通过（绿）**。
但照字面执行必然失败：

```
op=inspect 不传 arguments → code=agent_id_required
```

原因：schema 只校验 `op` 在不在枚举、`arguments` 是不是字符串，
**它不校验「参数有没有放进 arguments」。**

**所以那次「24 条全绿」是假通过。** 换成可执行性判据后才验出来。

---

## §0.13b 能精确替换就不要截断

**优先用 `old_string → new_string` 精确替换。**
只有确实要删掉一大段时才用截断，并且截断后必须做 §0.13 的三项验证。

**2026-11-18 同一天里，截断式改写造成了两次事故：**
```
第一次：companion_dsh_worker.py 丢了 _turn_error + __main__
        → worker 变空壳，dsh 全部"假成功"
第二次：companion_executor.py 加判据时 
 转义写坏
        → AST 预检失败，文件写坏（因为那次先 ast.parse 再写，才没上生产）
```

**落地的做法（第二次生效的）**：
```python
try:
    ast.parse(new_source)      # 写盘前先预检
except SyntaxError:
    raise SystemExit(1)        # 不写盘
io.open(PATH, 'w').write(new_source)
after = io.open(PATH).read()   # 写盘后复核
assert '__main__' in after or 原本就没有
assert 行数没大幅减少
```

---

## §0.12 两个不同的东西被当成了同一个

**今天五次统计口径错误，共同点都是这一句。**

```
1. `LIKE '%词%'` 当分类器
   它匹配"出现过这个词"，不匹配"这条记录是什么类型"。
   结果：13 条正常记录被当成失败、57 条被当成 48 条。

2. 字段不存在 = 字段为空
   `d.get('head') or ''`：没有 head 字段和 head 是空串，得到同一个值。
   结果：17 条正常记录被当成"空输出"。
   （同一形态的另一个例子：`kill(pid, 0)` 对僵尸进程也返回成功——查不到 ≠ 没在跑）

3. 历史统计 vs 实时决策的时间窗口
   用全库统计解释今天某一次失败，两者的时间范围不同。
   结果：把 14:08 的静默 cancelled 当成了 21:55 失败的原因。

4. 两张表混用
   `companion_agent_tasks` 有 verification 列，`companion_actions` 没有。
   结果：报成 8 条，实际 5 条。

5. 按时间对齐两张表，抓错了那一条
   decision 和 usage 用"最近的一条"对齐，抓到的是上一条 decision。
   结果：凭空造出"都不带 projects/actions 却输出 16000"这条不存在的记录。
```

**两条具体规矩：**

```markdown
### 按时间对齐两张表时，打印实际配到的那一对，而不是只打印结果
错的：print(out_tokens)
对的：print(decision_created, usage_created, gap, out_tokens)
      gap 超过阈值就跳过，不要硬配。

### 任何汇总数字，都要能回答"这条数字是由哪几行算出来的"
统计脚本输出时把 source row ids 一并带上。
判据：任何一个异常值，都能一键查出它的原始行。
```

---

## §0.11 改了「怎么写记录」之后，旧记录怎么办

凡是改变了「某类记录怎么写」的修复，都要问两个问题：

1. **已经写进去的旧记录怎么办？**
2. **她读旧记录时，看到的是什么？**

三种处理方式，按优先级：

- **读时动态判定**（最好）：代码改了，读的时候按新规则解释旧数据
- **带审计回填**（次好）：写一次，留痕说明「这是补的，原因当时没记」
- **什么都不做**（最差）：代码和数据的说法会长期不一致

**绝不允许：静默覆盖历史数据。**

今天的三个实例：

```
verification=pending → 代码改成 unverified，5 条存量还是 pending
  → 处理：带审计回填，每行写一条「这行为什么验不了」

details={} → 代码加了 reason，289 条存量还是空
  → 处理：带审计回填，写「这条记录写于修复之前，失败原因当时没有记录」
         不编造原因，但明确陈述「当时没记」这个事实

项目状态 → 代码改了校验，旧行动还是 cancelled 无原因
  → 尚未处理
```

---

---

### §0.11 补充：回填之前，先确认原始数据是真的不在，还是记在了别处

**2026-11-18**：4 条「没有 `head`」的记录，模型原始输出**一直在 `details.content` 里**。
只看 `head` 字段就说「翻不出来」，`SELECT details` 一展开就找到了。

**判据：说「没记录」之前，先 `SELECT *` 或至少 `SELECT details` 看一眼这条记录有哪些键。**

```
「某个字段不在」 ≠ 「这个事实没被记录」
```

**和 §0.18 是一对：**
```
§0.18     同一个词指不同东西（self_output vs message）
§0.11+    同一个事实记在不同字段（head vs content）
```

**同一批数据的实测收窄**（下次查同类问题可直接定位）：

```
「reply is a required property」的触发条件，实测只剩一种：
  缺字段   → 4 条历史记录全是这种
  null     → 构造能触发，但历史 0 条
  空串      → 根本不触发（schema 只要求 type:string；历史 2 条 "reply":"" 均未报错）
```

**不是「三种都可能」，是「实测出来只剩一种」。**

---


## §0.8 `x or default` 之前，先问「不存在」和「是空值」要不要分开

**今晚两个坑，形态完全相同：**

```python
# 坑 1（P47）：把"字段不存在"当成"字段为空"
head = d.get('head') or ''        # details 里没有 head 字段 → 空串
# 于是 17 条正常记录被当成"空输出"，
# 基于这个错误分类，得出了"21 次空输出、19 次与额度无关"的错结论

# 坑 2（进程检查）：把"查不到"当成"不在跑"
kill(pid, 0)                       # 对僵尸进程也返回成功
# 于是"进程还在"的判断对已经死掉的进程也成立
```

**共同点：两个不同的状态在代码里变成了同一个值。**

**写 `x or default` / `get(k) or default` / `is not None` 之前，先问一句：**

> **「不存在」和「存在但是空值」，在这里是不是需要分开处理？**

需要分开时，用显式的哨兵而不是 `or`：

```python
_MISSING = object()
head = d.get('head', _MISSING)
if head is _MISSING:
    ...            # 字段根本没来：这类记录不适用
elif not head:
    ...            # 字段来了但是空：这才是真的空输出
```

---

## §0.9 按文本筛记录，先看分类再下结论

2026-11-18 实测教训：用

```sql
SELECT ... FROM companion_activity WHERE details LIKE '%被截断%'
```

**本意是找"截断失败"，实际把 13 条正常记录也捞进来了**——
那些记录的正文里恰好提到「上次跑到一半超了 token 上限」。

**`LIKE '%关键词%'` 匹配的是"出现过这个词"，不是"这条记录是什么类型"。**

**先分类，再统计：**

```sql
-- 第一步：看 text 字段，确认这一类记录长什么样
SELECT text, COUNT(*) FROM companion_activity
 WHERE details LIKE '%被截断%' GROUP BY text;

-- 第二步：只统计真正的失败记录（按 text 或 type 精确匹配）
SELECT ... WHERE text = '这轮没有可解析的 JSON，已按纯回复交付，未执行任何动作。';
```

**最简单的判据：同一个错误关键词，`GROUP BY text` 之后如果出现两种以上的文字，就说明筛选条件不对。**

---

## §0.7 同步范围必须显式声明

任何部署前必须写清：

- 我这次要改哪些文件（**精确路径列表**）
- 我这次**不会**碰哪些（尤其是 `mobile/android/assets/`）

**禁止**用 `tar` / `rsync` 整目录推送到含前端资源的位置，除非明确要更新那些资源。
**只推你改过的那几个文件。**

推送后立即用标记确认前端没被顺带冲掉（以 `index.html` 为例）：

```bash
for m in stopPhoneControl renderPhoneStatus pendingRequests renderRequests \
         talkAboutEntry renderTasks loadOlderHistory; do
  printf '%-20s %s\n' "$m" "$(grep -c "$m" /opt/qq-deepseek/mobile/android/assets/index.html)"
done
# 任何一项为 0，说明前端被覆盖过，先查清楚再继续
```

并同时记录 **之前/之后** 的 md5 与字节数，写进报告：

```
index.html  之前 59e78c04 (37056, 0.4.7 原版)
            之后 <md5>      (<size>, 含 t12+t17)
```

### 案例（2026-11-18，真实原因）

当晚发生过一次「文件神秘回退」：`index.html` 从 56529 变回 37056、
`companion_experience.py` 等模块被删除、服务反复 `ImportError` 崩溃。

**一开始怀疑是同步操作，查了半小时没有定论。**（其间一度把责任归到 t16 的
「只改服务端」部署上，这是错的——t16 只 `scp` 了 3 个文件，从未写过 `mobile/android/`。）

真因：当天启用的 `companion-watchdog` 判定「服务不健康」后执行了回滚：

```
10:59:57  rolled back: restored=15  removed=60
11:02:08  rolled back: restored=0   removed=0
11:05:49  rolled back: restored=1   removed=1
11:10:44  rolled back: restored=8   removed=561
```

它把源码恢复到一个旧快照（`115735ff`，0.4.7 发布版），并**删除快照里没有的文件**。

### 教训

1. **自动回滚 = 恢复 + 删除**，在没有版本控制的工作区上极危险
2. 排查「文件为什么变了」的第一动作是 `find <dir> -newermt "<时间>"`，
   **看谁在写，而不是猜谁在写**
3. 启用任何自动化之前，先问「它失败时会做什么」，不只是「它成功时会做什么」

---

## §0.5【根本解】共享文件要么单人串行，要么拆成独立文件

今晚文件冲突发生了 **5 次**，其中 3 次是"读全文 → 替换 → 整体写回"造成的。
`companion.py` 今晚被 t3/t4/t8/t9/t13/t14 六方改过，每次整体写回都可能带走别人的改动。

### 两条规矩（按有效性排序）

**① 拆文件（消除冲突，首选）**
新逻辑写进独立模块（如 `life/companion_p32.py`），共享文件只加**一次**最小接线。
`companion_p32.py` 拆出来后，t13 再没出现过冲突——这是唯一真正消除冲突的办法。

**② 单人串行（次选）**
共享文件同一时刻只允许一个人写。其他人要改，必须先跟持有者打招呼。

**③ md5 检查（只减小概率，不能消除）**
改前 `md5sum` + `wc -l` 记基线；改完立即 `git diff` 确认没带走别人；部署前 diff 云端版本。

### 反例（2026-11-18）
- 队长三次用 `read_text()` → `replace()` → `write_text()` 改 `companion.py`，两次带走 t13 的 5 处改动
- 教训：**整体写回是共享文件的第一号杀手**

---

## §2.6 断言必须证明「确实验到了」

`all(x.id for x in items)` 在 `items == []` 时**恒真**。

2026-11-18 t16 实测踩到两次：生活条目 id 那条验收，先插了种子行，但没满足生活视图自己的
`LIVED_SQL`（要求 `result.status` 在成功/失败集合里且 `completion` 不是 accepted/queued），
投影把它们全过滤，`today_preview` 是**空数组**——于是「所有条目都有 id」直接通过。
它通过的原因是**什么都没验到**。

**规矩**：凡是遍历集合断言性质的地方，**必须同时断言 `len(items) > 0`**，
否则数据没走到会显示「通过」，比报错更危险。

同类陷阱（同一个病，不同写法）：

| 写法 | 空集合时 | 危险 |
|---|---|---|
| `all(pred(x) for x in items)` | 恒 **True** | 数据没到 → 假通过 |
| `any(pred(x) for x in items)` | 恒 **False** | 断言「不存在 X」→ 假通过 |
| `sum(...) == 0` | 空集恒 0 | 假通过 |
| `count(*)` 一致性检查 | 0 == 0 | 假通过 |
| `if not found: continue` | 整段跳过 | 循环体一次没跑也算通过 |
| **查错主键**（下面的实例） | 查不到 → 空 | 假通过 |

### 第四个实例：查错 `agent` 会**静默查空**

```python
# 生产库的 agent 不是 'default'，是 'xiaoyu'
# 查错的 agent 会静默返回空，而不是报错
```

2026-11-18 t16 验证 `refs` 引用解析时，脚本按 `agent_id='default'` 查
`companion_activity`，解析器返回 `found: false`——**看起来完全是「引用解析坏了」**。
实际是脚本参数错，代码是对的。真正的值是 `xiaoyu`。

**这类比空数组更隐蔽**：空数组至少你还能看到一个 `[]`；查错主键返回的是
一个**语义正确的 `False`**，会被当成业务结论。

自查方式：**先独立确认主键值，再断言业务结论。**
```sql
SELECT DISTINCT agent FROM companion_activity;   -- 先看真实值，不要假设
```

自查一句话：**这条断言如果数据完全没来，会红还是绿？** 会绿就是假通过。

### 第五个实例：并发重定向写同一个文件 = 内容拼接

```bash
sudo -u companion ... probe_token.py > /root/token.txt     # 两个进程同时跑
```

结果 `/root/token.txt` 里是**两条 token 首尾相接**，取出来只有第一段能用；
后续请求连续 `401`，看起来像「认证坏了」，实际是**自己的配置文件被自己写坏**。

`>` 不是「覆盖写」的原子操作——它是**先 truncate 再写**，两个进程交错时后写的
会接在前写的残余后面。

**规矩**：任何会被多个步骤复用的临时文件，要么用 `mktemp` 生成唯一名，
要么让唯一的那个进程写，其余步骤只读。

**同类**：`>>` 追加到日志没有轮转、多个进程写同一个 sqlite 的 WAL、
两个 scp 同时推同一个目标文件。

---

## §2.7 给前端的状态词：`unknown` 不是「不可用」

`/mobile/state` 的 `capabilities` 里，`status` **原样**是
`ready` / `needs_setup` / `offline` / `unknown` 四种。

生产实测（2026-11-18）`action.web` / `action.command` / `action.work` 都是 **`unknown`**，
原因是 `/mobile/state` 的快照里 `available_actions` 只有执行器**当轮**报过的种类。

**`unknown` 的含义是「不知道」，不是「不能用」。** 渲染规则：

| status | 前端表现 |
|---|---|
| `ready` | 正常/可用 |
| `needs_setup` | 黄色：还没配好，但可以配 |
| `offline` | 红色：配好了但掉了 |
| **`unknown`** | **灰色/中性：「状态未知」——绝不能用红色** |

画成红色会让用户以为那三样都坏了，而实际只是这一轮没探测到。
**把「不知道」画成「坏了」，是把不确定性变成假消息。**

---

## §0.7b 部署后必须做的两件事（`systemctl is-active` 不够）

`systemctl is-active` 在 `activating` 时也可能返回非 0，而很多脚本只 `grep active` 就当成功了。
2026-11-18 就是靠**真实 import** 才抓到 `companion_experience.py` 被删导致的
`ImportError`。

```bash
# 1) 真实 import，不是 py_compile
sudo -u companion env -i HOME=/var/lib/companion PATH=/usr/local/bin:/usr/bin:/bin \
  PYTHONPATH=/opt/qq-deepseek .venv-companion/bin/python -B -X utf8 \
  -c 'import life.companion; print("IMPORT_OK")'

# 2) 本地有、云端有没有少文件（回滚/同步都可能删文件）
for f in $(ls life/*.py); do
  ssh root@host "test -f /opt/qq-deepseek/$f" || echo "MISSING $f"
done
```

**注意**：`systemctl is-active companion-watchdog` 也要看一眼。2026-11-18 10:36 启用的
自动回滚守护在 4 次触发里 `removed` 了 60/0/1/561 个文件——**回滚 = 删掉快照里没有的文件**，
它把当晚所有未合入快照的改动删了。**在多人并发修改的代码库上，不要启用会写源码目录的守护进程。**

---

## §0.7c 云端同步工具：到底是什么、在哪、怎么用

**前面几份文档里引用的 `tools/stage-b/safe_put.py` 在云端并不存在**（2026-11-18 实测
`ls` 无此文件）。以为它存在会让「用 safe_put 逐个推」这条安全流程落空。

现存的逐个文件推送工具是 `tools/safe_put.sh`（本轮新增）：

```bash
bash tools/safe_put.sh life/companion.py     # 只会传这一个文件
```

它做三件事：
1. **拒绝目录**（`REFUSED: ... is a directory`）——从机制上堵住整目录推送
2. 推送前 `cp -a` 备份成 `<file>.bak-safeput-<stamp>`
3. 打印 `DECLARED <md5>  ACTUAL <md5>  MATCH|MISMATCH <path>`

**不要**再用 `tar czf - . | ssh ... 'tar xzf -'`：它覆盖目标目录下的一切，
且删除目标端多出来的文件。
