
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
