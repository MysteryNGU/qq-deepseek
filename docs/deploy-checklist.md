# 部署检查清单（每次改动 `life/` 或 `companion.py` 必跑）

> ## ⚠️ 第一句：`systemctl` 显示 `active` **不算通过**
>
> 进程活着和她在说话是两件事。今晚至少三次「服务 active、实际全坏」：
>
> | 时刻 | `systemctl` 显示 | 实际情况 |
> |---|---|---|
> | 00:01–00:08 | active | 每轮对话全崩（`_messages` 两处 `AttributeError`） |
> | 23:51 | active | 崩溃循环 7 次 |
> | 23:57（磁盘满） | active | 写库全失败 |
>
> **判断"部署成功"只有一个判据：真实 `_messages()` 产出非空请求体。** 见 §2.2。
> 只看前三行就动手的人，请直接跳到 §2.2 跑那一行命令。

日期：2026-11-18。作者：experience-engineer（t4）。
来源：2026-11-17 一晚两次"看起来部署成功、实际每轮崩"的事故复盘。
**这份清单不是文档装饰，是每次部署的必跑项**；每条都给了可直接粘贴的命令和"不通过长什么样"。

判据口径（队长 2026-11-18 采纳为团队规矩）：

> **部署后必须有一次真实 `_messages` 产出非空请求体，才算上线成功。**

依据是下面这张能力表——前三项都测过，全部漏过了今晚的两个故障：

| 检查 | 能发现什么 | **漏掉什么** |
|---|---|---|
| `systemctl is-active` | 进程活着 | 每轮都崩也显示 active |
| `py_compile` | 只有语法错误 | 类型错、名字错、模块缺失 |
| `import life.companion` | 只有模块缺失 | 函数/方法调用形状错 |
| **真实 `_messages()` 跑一次** | tuple/dict、方法/函数、模块缺失**全部暴露** | — |

---

## 0. 今晚的两个真实故障（为什么要这些条目）

| # | 故障 | 形态 | 哪条检查能抓到 |
|---|---|---|---|
| A | 漏传文件 | `companion_experience.py` 没同步到目标机 → `ImportError` 崩 3 分钟 | §1「被 import 的模块都在」 |
| B | 调用形状错 | `experience.summary(kinds)`——`summary` 是 `Experience` 的方法，模块级没有 → 每轮 `AttributeError` | §2「真实 `_messages`」 |
| C | 类型不匹配 | `unavailable` 是 tuple 列表、消费方按 dict 读 `item.get('kind')` → 每轮 `AttributeError` | §2「真实 `_messages`」 |

**B 和 C 在同一段 6 行里，且 C 在 B 之后执行——只修一个她照样不出声。**

---

## 1. 部署前

### 1.1 被 import 的模块是否都在目标机（防故障 A）

新增模块时最容易漏。**先列出"这次动了哪些文件"，逐个确认目标机存在**：

```bash
# 在本地/源树执行：这次改了哪些文件
git status --porcelain life/ companion.py

# 在目标机执行：逐个确认存在且非空（以新增模块为例）
for f in life/companion_experience.py; do
  ls -l /opt/qq-deepseek/$f || echo "MISSING: $f"
done
```

不通过长这样：`MISSING: life/companion_experience.py` → **必须补传，不能启动**。

补一条更狠的：直接让 Python 把整棵 import 树走一遍（能抓出"文件在但依赖它的模块不在"）：

```bash
cd /opt/qq-deepseek
.venv-companion/bin/python -c "import life.companion, life.companion_context, life.companion_experience; print('import ok')"
```

注意：**`import ok` 只能证明模块在，不证明调用点对**（故障 B/C 就是 import 全过）。

### 1.2 共享文件：同一时刻只允许一个 agent 写

今晚 `companion_awareness.py` / `companion_context.py` / `companion.py` 三个文件被多方并发改，
靠"改完各自核对"才没互相覆盖。规矩：

- **动 `companion.py` 前先声明**（谁在改、改哪几段），改完报「已落盘」；
- 整段字面替换（`edit` 大块）**最容易覆盖别人的并发改动**；小步改，改完立刻复查自己的标记还在不在；
- 改完自查（以 t4 为例，我的 5 处标记）：

```bash
grep -n "experience_log\|op == 'used'\|_WINDOW_UNPARSED\|def _used" /opt/qq-deepseek/life/companion*.py
```

### 1.3 磁盘：不得 `copytree` 生产数据目录

`/var/lib/companion/xiaoyu/data/life.db` 是活库（审计时实测 386 MB）。任何验证脚本、备份、
对拍都**只读打开**，不要复制整个 `data/`（复制过程中活库在写，副本天然是坏的，
且会撑爆磁盘）。只读打开的正确姿势：

```bash
.venv-companion/bin/python - <<'PY'
import sqlite3
con = sqlite3.connect('file:/var/lib/companion/xiaoyu/data/life.db?mode=ro', uri=True)
print(con.execute('select count(*) from companion_actions').fetchone())
con.close()
PY
```

任何脚本里出现 `shutil.copytree` / `cp -r /var/lib/companion` **一律打回**。

### 1.4 权限：确认目标机路径可写、属主正确

单位文件写的是 `User=companion` / `UMask=0077`（`deploy/companion/companion.service`）。
传文件后属主容易变成 root：

```bash
stat -c "%a %U:%G" /opt/qq-deepseek/life/companion_experience.py
# 期望：644 companion:companion（或至少属主 companion、进程可读）
```

### 1.5 【P0】从临时目录跑脚本：先清 `sys.path`，或用 `python -I`

**这不只是部署验证的坑——任何"把一个 `.py` 丢进杂目录再执行"的场景都会中招。**

`python /tmp/x.py` 会把 `/tmp` 当成 `sys.path[0]`。临时目录里经常残留
`ast.py` / `repo.py` / `env.py` / `json.py` 这类调试文件，它们会**顶掉标准库或业务模块**：

```
File "/tmp/ast.py", line 4, in <module>          ← 标准库 ast 被 /tmp/ast.py 顶掉
FileNotFoundError: [Errno 2] No such file or directory: '/opt/qq-deepseek/life/companion.py'
```

（`asyncio` 依赖 `ast`，所以表现成一个和本次改动**毫无关系**的诡异报错。）

**为什么是 P0**：它会让验证者**怀疑错的对象**——去查代码，而实际是环境脏。
和"断言失真"是同一类病：一个把坏的看成好的（r2_escape 判据被 syslog 关键词误判成通过），
一个把好的看成坏的。

**规矩（二选一，必须做）**：

```python
# ① 脚本开头自己清（推荐，不依赖调用方式）
_HERE = str(pathlib.Path(__file__).resolve().parent)
while _HERE in sys.path:
    sys.path.remove(_HERE)
sys.path.insert(0, TREE)
```

```bash
# ② 调用时用 -I（隔离模式，不把脚本目录/用户 site 放进 sys.path）
python -I /tmp/x.py
```

`_t4_regression_live_round.py` 已内置 ①，两种方式都实测通过。



### 2.0 一行命令（贴给所有改 `companion.py` 的人）

```bash
# 部署后必跑：真实 _messages 必须产出非空请求体
cd /opt/qq-deepseek && sudo -u companion \
  /opt/qq-deepseek/.venv-companion/bin/python -I /tmp/_deploy_check_live_round.py /opt/qq-deepseek \
  | grep -q '"has_experience_section": true' && echo "DEPLOY_OK" || echo "DEPLOY_FAILED"
```

**判据**：输出 `DEPLOY_OK`。等价于人工确认 `dialogue` 与 `background` 两条都是
`ok: true` 且 `has_experience_section: true`。看到 `DEPLOY_FAILED` **立即回滚**。

三个要点（都是实测踩出来的，别改成"看起来更稳"的写法）：

- **`-I` 与 `PYTHONPATH` 不要同时用**。`-I` 会同时忽略 `PYTHONPATH` 和当前目录；
  脚本自己会把代码树插进 `sys.path`，**不需要 `PYTHONPATH`**。同时写两者只是自相矛盾。
- **`sudo -u companion`**：unit 里是 `User=companion`，用 root 跑会在 `/root` 下建临时目录，
  与被验证的实际运行身份不一致。
- **不要 `cd` 到脚本所在目录再跑**（尤其别把脚本丢进 `/tmp` 就在 `/tmp` 里跑）——见 §1.5 的 P0 坑。

### 2.1 重启（改文件不重启 = 没改）

```bash
systemctl restart companion.service
sleep 5
systemctl show companion.service -p ActiveState -p NRestarts -p MainPID
```

只看 `ActiveState=active` **不算通过**——今晚崩着的时候它也是 active。

### 2.2 真实 `_messages` 跑一次（防故障 B/C，**这是唯一能拦住今晚两个故障的检查**）

用现有脚本，真 `Companion` + 真 `Config` + 真 sqlite + 模型注入打桩，**真调 `_messages`**，
对话轮与后台轮各一次。

脚本源：`.review-server/_t4_regression_live_round.py`（本地仓库）。
**它不依赖任何本地路径**：只 import 目标树里的 `life`，其余全在临时目录里跑。
部署时把它放到目标机的代码树之外（例如 `/tmp/_deploy_check_live_round.py`），不要放进
`life/` 或被 unit 引用的路径：

```bash
scp .review-server/_t4_regression_live_round.py <host>:/tmp/_deploy_check_live_round.py

# 在目标机执行（参数是代码树根；不写则默认脚本所在树的上一级）
cd /opt/qq-deepseek
.venv-companion/bin/python /tmp/_deploy_check_live_round.py /opt/qq-deepseek
```

脚本只在临时目录里建 home 与 sqlite，**不读不写 `/var/lib/companion`**，可以对着生产机放
在位跑。

> **实测踩到的坑（2026-11-18 00:20）**：脚本拷到 `/tmp` 直接跑会报一个和本次改动毫无关系的错——
> 临时目录里有别人遗留的 `ast.py` / `repo.py` / `env.py`，而 `python /tmp/x.py` 会把 `/tmp`
> 当成 `sys.path[0]`，这些同名文件直接把标准库 `ast`（`asyncio` 依赖它）顶掉：
> ```
> File "/tmp/ast.py", line 4, in <module>
> FileNotFoundError: [Errno 2] No such file or directory: '/opt/qq-deepseek/life/companion.py'
> ```
> 脚本已经在内置处理（import 前把自己所在目录从 `sys.path` 摘掉），但**手工写别的验证脚本时
> 同样会中招**。两个稳妥做法：①脚本开头做同样的 `sys.path` 清理；②用 `python -I` 跑。
> 这条同样适用于任何"把一个 .py 丢进杂目录再执行"的场景。

**通过判据（断言点）**：

```json
"dialogue":   {"ok": true, "messages": 4, "has_experience_section": true}
"background": {"ok": true, "messages": 4, "has_experience_section": true}
```

- `ok=true` → 没抛异常；
- `has_experience_section=true` → 经历段真的进了请求体（说明整条 state 拼装走到了）；
- `messages >= 2` → 那条 system + 生活材料都在。

**不通过长这样**（今晚实际出现过的）：
```
"error": "AttributeError: module 'life.companion_experience' has no attribute 'summary'"
"error": "AttributeError: 'tuple' object has no attribute 'get'"
"error": "AttributeError: 'str' object has no attribute 'text'"
```
任何一条出现 → **回滚，不要让它对着生产跑**。

### 2.3 模型调用是否真的发生（最终判据）

`_messages` 通过只说明请求体能拼出来；还要确认真的在调模型、真的在出声：

```bash
journalctl -u companion.service --since "5 min ago" | grep -c "认知失败"     # 期望 0
```

以及看 `companion_activity` 最近有没有新的非 error 记录（只读）：

```bash
.venv-companion/bin/python -c "
import sqlite3
con=sqlite3.connect('file:/var/lib/companion/xiaoyu/data/life.db?mode=ro',uri=True)
for r in con.execute(\"select type,text,created from companion_activity order by created desc limit 10\"):
    print(r)
"
```

不通过长这样：`认知失败，已保留输入并停止立即重试` 每 2 秒一条
（今晚 00:05–00:11 的实际形态，`NRestarts` 还是 0、服务还是 active）。

### 2.4 单测

```bash
cd /opt/qq-deepseek
.venv-companion/bin/python -m unittest life.tests.test_companion_experience life.tests.test_companion_awareness
```

注意：**单测全绿 ≠ 生产能跑**（今晚 20 项全绿仍然每轮崩），
它只能证明"被测到的那部分是对的"。所以 §2.2 不能省。

---

## 3. 出问题时的定位顺序

按这个顺序查，别跳：

1. **先拿完整 traceback**：

   ```bash
   journalctl -u companion.service --since "2 min ago" | grep -B2 -A15 Traceback
   ```

2. **以 traceback 的最后一行 `文件:行号` 为准** —— 不是以审计表/journal 里
   `details.location` 的外层函数为准。

   > **行号也会骗人。** 今晚队长追 `_cognize:1811`，那一行其实是 `if self._closed:`；
   > 真正崩在 `_messages:1634`。**记录器记的是外层函数，不是故障点。**
   > 结构化的 `location` 只适合定位"哪条调用链"，不适合定位"哪一行"。

3. **手里有真实对象时，直接跑一次复现**，拿完整 traceback——不要从审计表的行号倒推。
   复现脚本见 §2.0，它会把 `ok` 与 `error` 一起打出来。

4. 看那一行的**实际类型**（`sed -n '行号p' 文件`）：是 tuple 还是 dict、是方法还是函数。

5. 检查同名符号有没有被另一处定义占用：`grep -n "def 同名\|同名 ="`。

6. 修完**从 §2.1 重来一遍**，不要"改完就宣布好了"。

今晚的错误示范：看到 `AttributeError` 就按"命名冲突"去查，结果去追了一个不存在的冲突——
**错误结论比不够准确更糟，它把人引向错误方向**。以 traceback 为准，以复现为准。

---

## 4. 一页速查

```
□ 1.1 新增/改动的文件都在目标机，且 import 通过
□ 1.2 共享文件改前声明、改后复查自己的标记
□ 1.3 没复制生产数据目录，一切只读打开
□ 1.4 属主与权限对（companion:companion）
□ 2.1 systemctl restart（改文件不重启 = 没改）
□ 2.2 真实 _messages：dialogue/background 都 ok=true 且 has_experience_section=true
□ 2.3 journalctl 无新"认知失败"，有新的正常活动记录
□ 2.4 相关单测全绿
□ 出问题：以 traceback 的行号为准，不猜
□ 验证脚本从杂目录跑：用 python -I，或先清 sys.path[0]
```

**记住一句**：`active` 只说明进程活着；`import ok` 只说明模块在；
**只有真实 `_messages` 产出请求体，才说明她能说话。**
