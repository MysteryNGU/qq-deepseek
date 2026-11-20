# 失败报告：三次判断全部撤回，真因是我查错了表（2026-11-20）

承接 `docs/commitment-chain-20261120.md` 与 `docs/failure-report-gap-20261120.md`。

**本文推翻前两份里关于 `reports` 的全部结论。**

## 一、我错在哪

```
我从第一轮查 "她报没报失败" 起，就假设 reports 存在 events 表里。

实际：
  events 表 kind='message' / source_kind='self_output'  → 她的输出文本，**没有 reports 字段**
  companion_activity type='decision' 的 details.content  → decision 原文，**reports 在这里**
```

**因为查错表，我连续三轮得出三个错误结论，并基于它们提了三个修法：**

```
第一轮："她填了 []，是判断错时间（下一轮报）"     → 提了 B'（改说明措辞）
第二轮："她被 ContinuityError 拦了，decision 被降级" → 提了 C1（放宽 readonly 白名单）
第三轮："project_ref 不匹配导致 reports 消失"      → 提了 C1 + C3
```

**三个修法全部针对不存在的问题。C1 甚至已经被批准"论证范围"。**

## 二、真实数据

```
全库 state='failed' 的 reports: 4 条
  evt_e2a8ad6b5ea34a37  'dsh 建 /tmp/probe-fail.txt 没成。真实原因：worker 起会话时被拒…'
  evt_9985525695104724  'dsh 建 /tmp/probe-fail.txt 没成：动作回执 failed、exit_1…'
  evt_e005001de29a49ab  '…ls 与 cat 都是 No such file，文件没落盘…'
  evt_3554299f481d4849  '…动作 status=failed、exit_1；ls 与 cat 都是 No such file…'
```

**这 4 条正是我逐一检查后判"reports=0"的那 4 轮。
她用的是正确的 `project_ref`（`companion-project_ad71af5d…`）和正确的
`basis_refs`（`['companion-action_ea320063e3efd493dc774d8b', …]`）。**

**她一直做对了。是我看错了地方。**

## 三、ContinuityError 那条是真的，但拦的不是 reports

```
09:48:46 / 09:49:19  activity: "这轮的状态更新与已保存事实冲突，已改为只交付回复与真实回执。"
                     reason: ContinuityError: …companion-action_ea320063e3efd493dc774d8b
```

**降级代码（`life/companion.py:3656-3670`）只清这些：**

```python
decision['projects'] = []
for key in ('commitments', 'engagement', 'drive_feedback'):
    decision.pop(key, None)
```

**`reports` 和 `actions` 不在清除列表里。被拦的是**项目状态更新**，不是失败报告。**

**所以 C1（readonly 白名单加 dsh）不需要做——她的引用走的是 `basis_refs` 直接按
action_id 查表那条路，根本不进跨项目 `readonly` 分支。这一点在论证之前就该先问。**

## 四、不追问的验证：她主动报了

**验证设计**（用户指定）：发一条会失败的任务，**之后不发任何追问**，看 3~5 轮内会不会主动报。

**结果（10:01:25 起，8 分钟内）：**

```
用户消息数：1（只有那条任务本身，之后 0 次追问）
她的输出：20 条
带 reports 的 decision：6 条（state='failed' 4 条 / 'observed' 2 条）

10:02:06  "收到，这回换成 probe-auto.txt。先看一眼上一轮挪目录到底动没动…"
10:03:36  "挪不动，那五个 root 的会话目录还在原地——mv 全 Permission denied…"
10:03:58  "小鱼又跑了一遍，还是没落盘——先别急着说 dsh 不行，这会儿真凶已经露头了…"
             ↑ 对应 reports state='failed'："dsh 建 /tmp/probe-auto.txt 再次失败，文件未落盘…"
10:04:51  "接着上一条：两个文件都没落盘，真凶是同一条——它去扫 root 留下的旧会话目录…"
10:07:10  "准话有了：不是 dsh 本身不行。它去扫 root 在 11-18 留下的一个旧会话目录，被 EACCES 挡在门外…"
10:07:43  "没成，probe-auto.txt 也没落盘。现场倒是干净了…"
```

**判据：不追问的情况下，她在动作失败后 2 分钟就报了，并且连续三轮主动更新进展。**

## 五、所以"交代的事要反复催"这个感受，来源不是"她不报"

```
她能主动报（本次实测 6 条 reports / 8 分钟 / 0 次追问）
她也能长期跟进（20 条输出，一路从挪目录查到 dsh_home 配置）

那用户为什么觉得要催？
① 上一份文档里的"主动率 21%"——那个统计用的也是 events 表，**同样不可靠**
② 她说的话散在多条消息里，"没成"两个字夹在长篇排查中间
③ 失败之后她没有"停下来汇报"，而是继续往下查——
   用户看到的是"她还在忙"，不是"她告诉我没成"
```

**第 ③ 条是我现在的推测，没有验证。**

## 六、这次错误的性质与教训

**不是"数据不够谨慎"，是**用错数据推动了改动**。**

```
第一轮假设 reports 在 events 里
→ 三轮推理全部建立在这个假设上
→ 三轮修法全部针对不存在的问题
→ C1 甚至已被批准"论证范围"
```

**唯一没造成损失的地方：停在改真代码之前。**

**要固化的查法（今早已经立过一次，今天又犯）：**

```
报告 "X 没发生" 之前，先确认 "X 在这张表里"：
  ① 从业务词反查字段：grep 这个字段名在哪些 SQL 里被 SELECT
  ② 找一个已知有 X 的样本，打印它的完整行，确认字段名与位置
  ③ 再写统计口径
不做第 ② 步就统计，等于用空结果证明"没有"。
```

## 七、撤回清单（前两份文档中作废的部分）

```
failure-report-gap-20261120.md §二 候选 A「_messages 没有未报告机制」  —— 未验证，保留为待查
failure-report-gap-20261120.md §二 候选 B「state 没有 failed」          —— 成立（已修，她用了 4 次）
failure-report-gap-20261120.md §二 候选 C「strict 抬高填写成本」        —— 未验证
failure-report-gap-20261120.md §三「7 天 0 条 reports」                —— **作废，查错表**
failure-report-gap-20261120.md §四「三层叠加的死状态」                  —— **作废**
commitment-chain-20261120.md §五 5.1「dsh 那条 2 小时 0 次提到」        —— **需按 decision 表重算**
```

## 八、仍然成立、值得做的

```
① state 枚举加 failed/blocked —— 已部署，实测她用了 4 次，这是真改善
② C3（被拦时要让她知道）—— 仍值得做，但对象是 projects 而不是 reports
③ 按 decision 表重算"主动告知率"—— 旧的 21% 不可信
```
