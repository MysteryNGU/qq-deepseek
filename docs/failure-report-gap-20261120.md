# 失败结果为什么没被带出来（2026-11-20 追加勘察）

承接 `docs/commitment-chain-20261120.md` §八「仍未处理的断点」。

## 一、问题

29 条 agent/dsh 的失败或取消动作（7 天），她之后 2 小时内的告知情况：

```
主动告知（0 条用户消息后就说）   6 条  21%
追问后告知                      21 条  72%
完全没提                         2 条   7%
```

最极端一条：`companion-action_4f89b8598`（dsh failed，`return_code=1`，
完整错误原文在 `result.output` 里），之后 2 小时内她说了 **120 条消息，0 次提到 dsh**。

## 二、三个候选根因，逐个验证

### 候选 A：`_messages` 里有没有把"未报告的动作结果"带进下一轮

```
grep 未报告 / unreported / pending_report / 待报告 / report_back → 无此机制
（命中的 unreported_attempts 是计费重试计数，与动作结果无关）
```

**结论：没有这个机制。但这不等于她看不到 —— `observation.action_result` 会带当轮结果。
问题是"当轮看到了，下一轮就不在了"。**

### 候选 B：`reports.state` 里有没有 failed

```python
# life/companion.py
REPORTS = {'type':'array','maxItems':4,'items':{'type':'object','additionalProperties':False,
    'required':['text','state'],'properties':{
        'text':{'type':'string','minLength':1},'project_ref':{'type':'string'},
        'basis_refs':{'type':'array','items':{'type':'string'}},
        'state':{'enum':['planned','running','observed','completed']}}}}
```

**枚举里只有 `planned / running / observed / completed` —— 没有 `failed`、没有 `blocked`。**

而校验处明确：

```python
if report.get('state') not in {'planned','running','observed','completed'}:
    raise DecisionStateError('reports.state无效，只能是planned/running/observed/completed')
if report.get('state')=='completed' and not report.get('basis_refs'):
    raise DecisionStateError('reports标记completed需要真实basis_refs，不把计划或受理当成已完成')
```

**所以：动作失败了，`completed` 不能用（没有成功回执），另外三个都不是"失败"。
她没有字段可以表达"这件事没成"。**

### 候选 C：strict_schema 让填 reports 变得更难

```
原始定义  items.required = ['text','state']
strict 后 items.required = ['text','project_ref','basis_refs','state']
```

（strict_schema 把每个 object 的所有属性都设成 required，这是 OpenAI strict mode 的硬要求，改不了。）

**后果：填一条 report 必须同时给 `project_ref` 和 `basis_refs`。
而失败轮次往往没有 `basis_refs`（没有成功产出可引用）——
填了被 schema 拒，不填也没人管。**

## 三、决定性证据：7 天 0 条 reports

```
7 天内所有 self_output（3087 条）：带 reports 的 0 条
29 条失败动作的后续轮次：带 reports 的 0 条
```

**这个字段从未被使用过。不是"她偶尔忘了填"，是整个机制处于死状态。**

## 四、真正的根因（三层叠加）

```
① 没有语义槽位：state 枚举里没有 failed/blocked，失败无处安放
② 填写成本高：strict 后每条 report 必须同时有 project_ref + basis_refs
③ 没有驱动：reports 不在顶层 required，不填不报错，也没有任何提醒机制

→ 三层叠加的结果是「不填」永远是最省事的选择
→ 而失败这件事恰恰最需要被说出来
```

## 五、这直接对应"交代的事不用反复催"

```
她的动作失败了
  → 没有字段可以报告失败
  → 下一轮的 _messages 不再带这个失败（只有当轮的 observation.action_result）
  → 她不知道上一轮失败过
  → 用户必须追问「那个事怎么样了」
  → 用户感觉「交代的事要反复催」
```

**"被催"不是她记性差，是链上有一环没有承载它的地方。**

## 六、修法（待确认后执行）

**最小改动，不动 strict mode，不动状态机：**

```
① REPORTS.items.properties.state 枚举加 'failed' 与 'blocked'
   → 让失败有地方说
② 对 failed/blocked 放宽 basis_refs：失败时以 action_id 作为 basis_ref 即可
   （现有的 _validate_report_basis 已经在做 basis 校验，复用它）
③ 补一条 GUIDES/reports 说明：动作回执是 failed/cancelled 时，
   用 state=failed 报出来，basis_refs 填 action_id
```

**风险：这改的是 DECISION schema，属于模型协议层。
按 `docs/superpowers/specs/2026-11-19-final-issues-amendment-design.md` §8，
协议变更要走"先恢复证据再改协议"的流程，不应与其它改动绑在一起上线。**

**所以我没有直接改，先报。**

## 七、附：本轮之前两处的口径更正

```
① 上一轮我判"3 条动作失败但她都如实告知了" —— 错。
   其中 dsh 那条（evt_6ed800b0）之后 2 小时 120 条消息里 0 次提到。

② "27/29 提到了"这个数字不能说明坦诚度 ——
   27 条里 21 条是用户追问后才说的，主动率只有 21%。
```
