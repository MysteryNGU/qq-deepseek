# 2026-11-19 上午 调查结果

**承接用户三处纠正后的重新定性。**

## 一、我要先撤回的三个结论

```
✗ "并发两路就开始失败"        → 实测 2/4/8/16 路：40 次、100 次全成功，零失败
✗ "fence 闸门把所有 job 挡住" → 队列在动：done 13075→13091，已索引 27548→27626
✗ "computer 20% 成功率"       → 本次未独立验证，撤回
✗ "dsh 只认官方 key"          → 本次未独立验证，撤回
```

## 二、B③ 索引为什么"处理一条失败一条"

**机制：8231 单进程，重压下请求排队，超过 30 秒超时 → `HTTPError` → 记成 `unknown`。**

实测（8 路并发 × 每个请求 30 条 × 200 字，等于索引一个资源的真实负载）：
```
耗时 min=2878ms  max=31231ms
超过 30 秒的：1 条     ← timeout_seconds=30
```

进程：
```
/opt/xiaoyu-embedding-venv/bin/python -B -m life.local_embedding \
  /opt/qq-deepseek/data/models/bge-small-zh-v1.5 3f3c57f7... 8231 2
```

**对照：手跑一条失败的 job（`memory-index_22ff96a47bcfe98dfaefe7b5`）→ build 成功。**
**资源本身没问题，是时机问题（撞上重压）。**

### 8231 的真实输入上限（代码里 `max_input_tokens=8192` 与实际不符）

```
单条最大字符数：505（511 失败）
批量最大条数：  32（33 失败）
超限返回：HTTP 400 {"error": "invalid_embedding_input"}
```

**`chunk_tokens=240`，实测切出来的片段最长 217 字——不超限。所以超长不是失败原因。**

## 三、B④ memory 的 failed 确实是"查不到"

```json
{"status":"failed","op":"search","query":"近视","results":[],
 "output":"没有匹配「近视」的动作。换个词，或者不带 query 看最近几条。"}
```

**7 条 failed：4 条 search 无结果、2 条 read 找不到 id、1 条 read 无内容。**
**全是"没有"，没有一条是"坏了"。**
**这是 §2.7「unknown 不是不可用」的同类反例——"查不到"被记成了"失败"。**

## 四、A① growth 的 op=add —— 不是 bug

**错误信息本身清楚且正确：**
```
"growth 的 op 支持 note/list/revise/drop/log/restore/review，收到的是 add"
```

**`GUIDES['growth']` 里教的确实是 `op="note"`。程序侧没问题。**

## 五、A② agent 的 node=work —— 报错是对的，但发现了更大的问题

**代码有明确校验和提示（`companion_agents.py:874`）：**
```
'node=work 要在 command 里写具体命令（例如 command="pytest -q" 或 command="python build.py"）'
```

**但 agent 的 27 次调用里：**
```
completed 7 / failed 13 / cancelled 7

  4  AgentManager.inspect() got an unexpected keyword argument 'project'    ← 代码 bug
  3  node=work 要在 command 里写                                           ← 说明/习惯
  2  Permission denied: /var/lib/companion/xiaoyu/data/companion/agents/shared  ← 权限 bug
  1  AgentManager.create() got an unexpected keyword argument 'kind'       ← 代码 bug
  1  AgentManager.inspect() got an unexpected keyword argument 'goal'      ← 代码 bug
  1  op 只有 create/send/... 收到 'tools'                                  ← 说明/习惯
  1  同一个请求已经登记过                                                   ← 正常去重
```

**7 条 `unexpected keyword argument` 是实打实的代码 bug——调用方传了方法签名里没有的参数。**

### 而且有一个直接断链的地方

```
GUIDES 的 key：
  ask / bilibili / body / command / computer / dsh / growth / memory /
  phone / skill / task / town / web / work
                                          ↑ 没有 agent
```

**`available_kinds()` 会返回 `agent`（她能"看得到这个名字"），
但 `GUIDES` 里没有 `agent` 这一条（她查不到 op 有哪些、参数怎么写）。**

**这正是团队目标里那条链断掉的位置：「已实现能力 → 可发现说明」在 agent 这一环是断的。**

## 六、建议顺序（按证据强度）

```
1. 给 GUIDES 加 agent 一条
   —— op 列表（create/send/inspect/wait/cancel/resume/read_artifact）
   —— node 的取值（echo/work/dsh），node=work 要 command
   —— agents/shared 的权限事实
   —— 这是"能力有、说明无"的直接缺口，也是团队目标的核心

2. 修 7 条 unexpected keyword argument
   —— 先 grep AgentManager.inspect / create 的真实签名，再改调用方
   —— §0.15：提到方法名之前先 grep

3. 修 agents/shared 的 Permission denied

4. "查不到"不该记 failed
   —— memory search 无结果应记 completed 并把 results=[] 说清楚

5. 8231 的重压超时
   —— 把 timeout_seconds 从 30 调高，或给 build 加限流
```

## 七、我这一轮的测量方法

```
py-spy record（不是 dump）→ 拿到精确占比
逐个放宽 WHERE             → 找到哪一道闸门
串行 vs 并发 vs 重压        → 区分"坏"和"慢"
手跑一条失败的 job          → 区分"资源有问题"和"时机有问题"
```
