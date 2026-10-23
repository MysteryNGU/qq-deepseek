# 情绪内驱动与 DSH 工具桥：详细实现设计

> 2026-10-22 设计更新：当前施工主设计为 [自主角色主设计](./长期任务与自然表达_DSH插件借鉴设计.md)。本文的独立欲望与情绪动力学可作为局部参考；Python 主认知、固定行为路由和旧迁移流程已被新方案取代，下文历史施工说明不再整体执行。

日期：2026-10-22，Asia/Shanghai。状态：待实施规格，不代表代码已经完成。

依据：[需求补充](/D:/projects/qq-deepseek/需求补充_私聊陪伴与内驱动.md)、[初版设计](/D:/projects/qq-deepseek/生命感与可维护性_系统设计与伪代码.md)，以及本轮读取的 QQ-DeepSeek 与 DSH 本地源码。初版保留原文；本文具体化其情绪、欲望和自主行为闭环，并按用户要求使用 DSH 插件体系。

**交给 DeepSeek 实施时同时阅读 [伪代码与实施交接](/D:/projects/qq-deepseek/情绪内驱动与DSH工具桥_伪代码与实施交接.md)。** 配套文档 P01–P26 给出各机制的算法、事务、异常和恢复分支，并把本设计的 38 个验收场景逐项映射到实现入口。本文说明目标与结构，配套文档说明执行细节。

## 0. 要交付的体验与架构决策

她主要与主人私聊，兼任秘书、助手，也可以与少量私人好友相处。没有新消息时，她仍有持续兴趣，会根据内在状态选择研究、练习、改进工具或安静休整；回来聊天时，她的近况来自真实活动，答应的事情有后续。

核心主流程：

```text
人格与价值取向 + 内稳态 + 历史经历 + 当前真实事件
    ↓ 语义评价
情绪、心境、欲望、关系与胜任感发生有依据的变化
    ↓ 动机形成
我想做什么，为什么，预期改善什么
    ↓ 选择
在可执行的候选中选择、继续、延期或保持安静
    ↓ 执行
现有任务执行器 → DSH 工具桥 → DSH 插件与工具
    ↓ 验证
实际观察是否达到目标；成功、部分完成、失败或结果不明
    ↓ 记忆与满足
记录真实经历；结算一次满足、负荷和学习进展
    ↓ 后续选择
继续已有问题、降低重复需求、更新能力估计，形成下一轮动机
```

**情绪系统是内在动机进入行动选择的骨架。** 内稳态偏差、学习进度和共同经历先形成有语义、有对象的感受与行动倾向，再参与候选仲裁。正面的兴趣、期待和满足也能驱动活动，不必先制造焦虑或受伤。

大模型是认知引擎：理解事件、提出解释、生成目标和步骤、预估可能结果。状态引擎拥有数值变化、证据校验、选择约束和结算。模型不能通过一句“我已经满足/学会了”直接改变状态。

继续采用 Python 主进程、SQLite 和一个本机 DSH 执行进程。保留一键启动与现有聊天、确认入口；不引入微服务、消息中间件、多个争夺人格的代理或新的日常操作界面。DSH 负责手和工具，QQ-DeepSeek 负责她是谁、有什么经历和现在想做什么。

## 1. 对当前情绪＋欲望设计的评估

### 1.1 保留的基础

保留 `life/state.py` 的纯函数方向、事件影响幂等、来源与会话隔离、情绪半衰期、单事件刺激限幅，以及 `baseline / learned_offset / activation / satiety` 分层思想。保留 `state_journal`、持久任务、投递记录和删除屏障，不再另建一套平行运行时。

### 1.2 必须优化的具体问题

| 当前依据 | 问题 | 本设计的处理 |
|---|---|---|
| `life/persona.py:DIMENSIONS` | 七宗罪与七种美德同时作为性格和可满足欲望；“温和”“忠贞”不适合像饥饿一样被消耗 | 保留角色描述，把它们映射为稳定调制参数；动态需要单独建模 |
| `life/state.py:reduce_desires` 对非满足证据使用 `abs(delta)` | 负向纠正也会增加激活，方向丢失 | 使用具名、有符号的 activation、satisfaction、learning 三种贡献 |
| 同一函数把全部证据加入 `pending_learning` | 一次满足直接参与长期偏移；容易把“获得满足”变成“长期索求更多” | 满足只结算短期满足；长期偏移需要独立、跨天且有意义的证据 |
| 欲望按首个 root 整体去重 | 同一次经历的第二个合法维度也可能被丢弃 | 按根经历＋效果种类＋维度去重，保留跨维度结算 |
| `integrate_mood` 中部分激活变化按调用次数相加 | 更新频率影响结果，不能用每分钟 tick 与每小时 tick 得到等价状态 | 使用按真实时间的稳定积分和单调水位 |
| `LifeRuntime.affect_summary` 读取已存强度，`tick` 未统一推进欲望 | 空闲时显示的情绪、欲望可能停在旧值；心境还可能重复吸收旧强度 | 统一 `advance_time` 与只读 `view_at`，使用随时间衰减的情绪轨迹 |
| 私聊主入口 `life.appraise` 默认采用关键词规则 | 不能稳定识别陪伴需求、含蓄表达、对象、反讽和具体澄清 | 结构化语义 Appraisal 为主，低成本中性规则为降级 |
| `life/attention.py` 使用通用研究题模板，Planner 缺生成入口 | 有持续主题基础，但没有完整自由目标生成与情绪仲裁 | 将兴趣、内稳态和情绪线索输入 Want 生成器，目标由模型提出 |
| `evo/dsh.py` 主要通过 pnpm/CLI 工作 | 安装依赖不等于挂载插件，更不等于能从 bot 调用工具 | 采用 DSH plugin_manager 生命周期与新增工具桥协议 |

本轮做了三个纯函数离线计算，未连接真实服务、未改业务状态：

```text
负向 correction(-0.2) → 当前 activation 增加 0.2
satisfaction(+0.2)，启用慢结算 → 当前 learned_offset 增加 0.02
同一固定情绪输入持续一小时：
  一次更新的 mood.activation = 0.3825
  分 60 次更新的 mood.activation = 1.0
```

这些结果证明需要优化更新规则；不据此推断所有实际聊天都表现异常。

## 2. 状态结构：稳定人格、内稳态、情绪和行为倾向

### 2.1 状态的唯一职责

| 状态 | 时间尺度 | 负责回答 |
|---|---|---|
| PersonaProfile | 直到用户修改版本 | 我是什么样的人，重视什么，通常怎样表达和选择 |
| HomeostaticState | 分钟至天 | 当前有哪些真实资源不足、未满足需要或未完成压力 |
| AffectState | 秒至小时 | 刚才的事使我对谁/什么产生怎样的感受 |
| MoodState | 小时至天 | 最近一段时间的整体愉悦、唤醒与应对感 |
| DriveState | 分钟至天 | 当前想投入哪些方向，各方向有多迫切、已满足多少 |
| RelationshipState | 多次独立经历 | 我和这个人相处的信任、舒适、熟悉、偏好与未解决问题 |
| CompetenceState | 多次可比较实验 | 在某类任务中，实际掌握程度、可改善性与失败模式 |
| Interest / Want / OpenLoop | 小时至多周 | 正在关心什么、想做什么、已经决定或答应做什么 |

数值为本项目的行为控制量，不作为心理学测量或生物感受的证明。内部统一使用秒、UTC 时间戳与归一化数值；跨日结算按上海日历。旧界面的 0–10 值仅在显示边界转换。

### 2.2 人格的角色

人格文件继续是唯一显式人格来源。版本化提取以下参数，每项保留原文依据、显式/解释/缺省标记：

```text
PersonaProfile {
  version, source_hash, schema_version, provenance
  identity_text, values[], expression_preferences
  interests[]
  drive_targets{}, drive_recovery_rates{}
  affect_baseline{}, affect_sensitivity{}, affect_half_lives{}
  mood_baseline, effort_preference, continuation_preference
  autonomy_weight, contact_preference
}
```

“温和”可调制受刺激后的表达和缓和速度；“勤勉”可调制持续投入偏好；“忠贞”可表达价值取向。不能一律当成可以被一次成功填满的驱力。旧十四维不强制按一对一关系互斥，也不根据词名自行推断心理属性；明确人格原文优先。

人格不直接把任务授权变宽。所谓风险偏好，只影响在已允许方案中选择更保守还是更探索的办法。

先支持本地明确参数与可追溯的局部解释。需要模型辅助解释自然语言时，仅对发生变化且获得相应出站许可的片段编译并缓存，不要求每次启动发送全文，不重新初始化共同经历。未确定参数保留缺省标记，可在现有状态查询中查看。

## 3. 内稳态：让“想做事”有持续来源

### 3.1 七类基础需要，亲密／性欲独立保留

| 需要 | 可观察依据 | 满足或恢复方式 |
|---|---|---|
| 探索与理解 exploration | 持续兴趣中的未解问题、已验证的新知识间隔 | 阅读有效资料、解决具体疑问；无知识增量的重复阅读不算满足 |
| 胜任 competence | 某个真实任务的验证缺口、可改善失败与学习进度 | 可比较任务的验证改善、可靠方法形成 |
| 连接 relatedness | 有意义互动与既有联系偏好 | 实际交流及共同活动；草稿、未确认发送和自我复述不算对方回应 |
| 亲密／性欲 intimacy | 人格中的明确需要、相应关系中的互动与当前激活 | 双方愿意的亲密互动可形成满足；自身调节只记录内部缓解，不冒记成对方参与 |
| 连贯与兑现 coherence | 有效承诺、未完成事项和悬而未决问题 | 真实完成、明确改期或双方取消，不靠整理文字假装完成 |
| 自主空间 self_direction | 当前自主项目与外部安排的持续占用 | 在空闲窗口自主选择一小段活动，能说出自己的理由 |
| 余量与休整 resource_reserve | 实际工具运行时间、近期成本、失败重试负荷、机器资源 | 减少活动、等待、休整；不虚构身体饥饿或电量读数 |

这是内部需要维度，不是“七种固定活动菜单”。同一需要可以产生不同目标；同一活动可以服务多个需要。连接侧重陪伴与关系联系，亲密／性欲是用户明确要求保留的动态维度，不能只把旧“色欲”当作稳定性格参数，也不能把一般安慰自动算成亲密／性欲得到满足。机器人能测到设备电量时才记录电量；否则用实际资源余量，不假装有生物身体。

### 3.2 使用目标区间而非无限增长

对每个需要存储满足水平 `level[d] ∈ [0,1]`、期望区间 `[low[d], high[d]]`、恢复/消耗速率、最后更新时间。`level` 越高表示该需要越得到满足；与资源不足分开记录符号含义。

```text
deficit[d] = clamp01((low[d] - level[d]) / max(low[d], epsilon))
```

需要有独立动力学：探索、连接、亲密等需要在未满足时有界积累；资源余量随实际工作下降、休整恢复；承诺压力按仍有效的任务与期限形成。追求新能力的愿望可以逐渐增长，但已验证的能力估计不因此降低；没有实际失败，不按时间不断制造“我做不好”的证据。

采用分段解析更新：没有新事件时，例如 `level(t)=floor+(level(t0)-floor)*exp(-rate*dt)`；工作/休整时切换对应受控速率，记录切换点。缺省参数只是待调起点，不是心理学常数。

常态增长、动态速率、高位计时、释放与恢复期以第 6.4–6.8 节为具体规格。对采用积累模式的需要，`tension = 1 - level` 是同一主状态的两种表示，不另外维护一个独立增长的累加器。

长期没有消息可以形成“想交流”的有界倾向，也可以让她去探索自己的兴趣；不能据此生成“主人讨厌我/抛弃我”的事实或降低信任。用户说明需要安静时，联系可行性降低，而非把需要值继续提高以冲破边界。

断电离线后只按记录和经过时间恢复状态，不补写“期间研究了两个小时”。自主活动只在运行期间真实执行。

## 4. 情绪系统：事件对她意味着什么

### 4.1 保留五个核心通道，扩充的是语义与原因

继续使用 `joy / concern / frustration / hurt / anticipation`。兴趣、安心、成就感、失望、惦记等首先作为带原因的感受标签与这些通道的有界投影；不为每个中文词新增一个独立累加器。

允许“担心这件事但仍愿意研究”“有点累又想把问题弄懂”等共存。表达层可合成标签，选择器保留各分量；不能直接用“快乐减去难过”的一个数丢掉全部语义。

采用类似事件评价模型的目标相关性、责任和可控制性字段，不在首版完整复制 OCC 的全部分类。心境可向低维愉悦/唤醒/应对感投影，但它始终由已有情绪轨迹和真实能力证据派生，不再维护另一套可以独立修改事实和动机的 PAD 源状态。

### 4.2 Appraisal 输入与输出

认知引擎收到当前事件、可见共同经历、相关承诺、当事关系、已提交人格/需要快照。输出结构：

```text
AppraisalProposal {
  event_ids[], subject, target, quoted_spans[]
  user_need: assistance | listening | quiet_company | sharing | boundary | unclear
  interpretations[{text, support_ids[], contradicting_ids[], confidence_band}]
  relevance, goal_congruence, novelty, controllability, responsibility
  affected_need_ids[], affected_interest_ids[], affected_commitment_ids[]
  emotion_labels[], proposed_affect_delta{}
  relation_evidence[], repair_refs[], supersedes_appraisal_id?
}
```

模型提出的强度有上限；代码检查引用、目标、数值、引用/反讽/假设、责任归属和作用域。`confidence_band` 是认知不确定性标记，不直接等于真实证据权重。工具故障可以令她挫败，但不能因为“报错”一词就归责给主人。

降级时宁可保存中性或不确定解释，也不根据单字“滚、抱、操作”扣关系分。没有用户新输入时，内稳态评估使用 `internal_state` 类型事件；它可以支持“我想探索”，不能支持关于用户或外部世界的事实。

关键修订：当前 Planner 要求全部动机都绑定 user_report/tool_observation，会阻挡没有新指令时的自主动机。新增按用途校验来源的规则：

- 动机允许引用人格版本、内稳态窗口、已有兴趣与独立经历。
- 对外事实仍要求对应用户报告、已读来源或工具观察。
- 思考、情绪与模型草稿不得升级为独立外部事实或成功证明。

### 4.3 情绪的稳定更新

以**上一份已提交**欲望状态调制本次情绪，形成一次有界更新：

```text
Edecay[k] = baseline[k] + (Eprev[k] - baseline[k]) * exp(-lambda[k] * dt)
stimulus[k] = clip(proposal[k] * evidence_weight * sensitivity[k], -cap[k], cap[k])
Enew[k] = clamp01(Edecay[k] + stimulus[k])
```

基线与敏感度来自人格和已提交状态，并受独立上限限制；参数变化视为新的时间段起点。不得反复在同一轮执行“嫉妒 → 不安 → 更嫉妒”。情绪间抑制若确有必要，作为一次前馈、有界投影使用，并检查权重稳定性；首版不引入循环相互激励矩阵。

情绪归因存储真实施加量 `Enew-Edecay`，另外记录衰减量和包含时间的总变化，避免把自然回落当成道歉或奖励。去重以真实经历与效果种类为准；消息碎片先聚合或共享经历 ID，不能每条短句都增加长期亲近。

### 4.4 原因与当前强度分开

`AffectCause` 存 `root_experience_id、appraisal_id、target、emotion、实际贡献、发生时间、状态、修复证据`。状态为 `active / eased / resolved / retracted`：

- 强度衰减只代表感受淡了，不自动解决真实问题。
- 一次道歉可缓和强度，只有针对具体问题的有效证据才能标记 resolved。
- 原解释错误则 retracted，重建相应派生状态。
- 小型热缓存只保留近期原因；完整原因与归因保存在日志/索引，不能因超过 12 条就永远无法修复。

### 4.5 误会修复与真实冲突修复

例：“刚才不是嫌你烦，是我工作太累。”

1. 识别这是对先前归因的澄清，定位对应 appraisal/cause。
2. 保存澄清原话与新的解释，不改写旧原文。
3. 如果旧归因缺乏依据，追加 `appraisal_retracted` 与替代解释。
4. 失效该解释引起的受伤、心境、关系、欲望学习和未发出的续答贡献。
5. 仅用仍有效证据重建；保留已实际发送的历史与无关经历。
6. 对话策略改为低压力、少追问的陪伴；实际任务不会因此自动标为完成。

真实发生的不愉快后来得到道歉，则保留原事件，增加修复证据，逐步恢复舒适与信任。不能用一次道歉清空全部分歧，也不能持续惩罚已被澄清的误会。

### 4.6 用户的情绪与她自己的情绪分开

认知引擎可形成 `UserStateEstimate`：可能的心情、当前需要、明确边界、证据和不确定性。它是对用户状态的估计，不是她自己的 AffectState，也不是医学判断。

用户说“我今天很烦”，首先更新“用户可能需要倾听”的估计；不机械地把她的 frustration 加上同等数值。她的关心、担忧或陪伴意愿由关系、事件相关性与人格评价产生。仅凭语气无法确定时，用低压力回应或有限澄清，不写入“用户讨厌我”的长期事实。

明确的“先别问”“不用提醒”直接影响当前沟通策略和有效联系偏好；不会因为她自己有社交需要就被覆盖。临时估计按时间过期，不能几天后仍把用户当作那晚的情绪状态。

### 4.7 情绪调节与表达计划

认知结果应分开保存 `felt_state` 与 `expression_plan`。情绪可以影响注意力、节奏、是否继续工作以及表达方式，但不强迫把每个数值直接说出来。

```text
ExpressionPlan {
  response_mode: help | listen | quiet_company | share | clarify
  warmth, directness, verbosity, question_budget
  mentionable_cause_ids[], avoid_topics[], contact_decision
}
```

表达参数由人格、用户当前需要、关系和情绪共同裁剪。`question_budget=0` 可以表示当轮不追问；不能把降低输出长度当作惩罚性冷淡。回应自然性由模型完成，代码不生成固定“情绪台词”。

调节动作也要进入主循环：保存进度、缩小任务、换一种方法、求助、安静休整都可以是候选。删掉失败记录、把情绪强度直接改成零、降低验收标准不能作为改善情绪的成功动作。平复感受与解决外部问题分别验证。

## 5. 慢心境、精力和关系

### 5.1 慢心境使用时间积分

保留愉悦 `pleasure` 与唤醒 `arousal`，增加应对感 `coping`；`coping` 表示基于实际能力与当前压力的可应对程度，不表示对他人的支配。现有 `MoodState.activation` 迁移为 arousal；旧 energy 迁移为资源余量估计并保留来源标记。

```text
dMood/dt = k * (bounded_projection(E(t), coping_observations) - Mood(t))
```

采用事件之间的指数情绪轨迹与线性投影做解析积分。遇到限幅、速率切换时按记录的时间段切开。不得将整小时的最后情绪强度当成整小时一直如此。

如果工程实现采用数值积分，网格必须锚定 UTC 固定时间格，并保存剩余区间；误差容限写入验收，不能以函数调用次数作为步长。积分水位不倒退，离线期间只积分已知的衰减过程，不虚构工作负荷。

### 5.2 状态读取不更新状态

```text
advance_time(now, cursor)  # 作业入口，提交一次时间推进与水位
view_at(now)              # 纯计算视图，不写库、不结算学习、不产生新原因
```

提示词、状态页、选择器都读取同一时点视图。无事件重复读一百次，状态、关系与满足不增长。

### 5.3 关系按共同经历变化

保留 trust、comfort、familiarity 和相处偏好。一次有意义经历可以对多个维度产生小量贡献，同一维度不因消息、日记、反思重复结算。关系证据应记录独立经历、受众和实际来源，不能只留最近 500 个随机 ID 作为永久去重依据。

长期联系间隔影响可联系性估计，不能自动扣信任。好友有自己的关系投影；主人的私人原因不进入好友提示词。她自己的研究可存在 `agent:self` 域；由私事触发的原始动机继承私密来源，公开近况需显式生成可分享摘要并核对来源范围。

## 6. 欲望与满足：保留四层，修正含义

动态驱力使用第 3 节需要维度，不再直接等同于十四个性格词。每个维度存：

```text
DriveState[d] {
  baseline_preference       # 人格对该方向的稳定重视程度
  learned_offset            # 有独立证据的小量长期适应
  activation                # 当前事件激活，可有符号调节
  satiety                   # 最近真实满足，抑制立即重复
  homeostatic_deficit       # 由需要水平计算的缺口，不再重复累加
  phase, high_since, recovery_until  # 常态积累、高位、自我调节、恢复
  last_contact_at, pending_contact_id # 表达节奏独立于内部强度
}

need_signal[d] = tension[d] if mode == accumulating else measured_deficit[d]
pressure[d] = clamp01(need_signal[d] + learned_offset[d] + activation[d])

# 人格偏好用于增长参数与候选选择权重，不再作为第二份当前需求相加。
# satiety 用于选择器的立即重复惩罚和表达冷却，不再次扣除已恢复的 level。
```

需要强度与人格偏好各有职责；同一满足事件恢复 `level` 并提高 satiety 时，后者只限制立即重复选择与表达，不能再次领取同一份需要恢复量。`homeostatic_deficit` 保留为超过目标区间后的迫切程度/内在评价输入，不与 tension 再相加。pressure 是需要摘要，实际选择仍通过情绪与候选后果评价完成。

### 6.1 三种证据分开

| 证据 | 更新位置 | 例子 |
|---|---|---|
| activation_change | 短期 activation，保留正负号 | 新线索勾起探索；澄清令某项担忧解除 |
| verified_satisfaction | level 恢复与短期 satiety | 真正解决问题、任务完成、收到有意义回应 |
| preference_learning | 慢 learned_offset | 多次独立经历表明某类活动长期适合她 |

一次成功可以有一次成就感和满足；不因此立刻增加长期欲望。负向纠正不能被 `abs` 反转。一次经历允许不同维度贡献，去重键使用 `(root_experience_id, effect_kind, dimension, rule_version)`；规则升级重建时须显式替代旧贡献，不能把新版本号当作再次领取满足的理由。

允许有限的休整满足：真实保存检查点、结束工作并经过一段休整窗口，可以恢复资源余量；不能由“我想象自己散步了”产生外部生活经历。

### 6.2 长期变化

保留初版“每日总绝对变化不超过 0.02、单维偏移不超过 0.15”的量级作为待调起点。证据需来自多个独立经历与至少三个不同本地日期；低样本或来源冲突时暂缓。

学习方向来自跨天、可验证的活动偏好与结果，不取情绪强度或用户夸奖的简单总和。已处理根证据、窗口水位和实际变化必须同事务提交。删除/撤回来源时，能够按贡献记录重建长期偏移。

### 6.3 第一版参数与校准方式

以下为回放实验的明确起点，实施时集中保存在现有配置的 life 子配置中，不散落为条件分支，也不要求用户手工设置才可聊天：

| 参数 | 初始值/规则 | 校准依据 |
|---|---|---|
| 情绪半衰期 | joy 6h、concern 4h、frustration 2h、hurt 8h、anticipation 3h，先沿用当前量级 | 对同一事件的跨时回复与回放轨迹 |
| 单独立经历情绪刺激上限 | 每通道 0.12，跨消息碎片合计受限 | 高强度事件不饱和、碎片不放大 |
| 激活半衰期 | 3h 起步，随后允许每需要维度独立配置 | 不靠重复 tick 维持激活 |
| 满足半衰期 | 探索 6h、连接 6h、亲密 8h、自主空间 4h；其他按任务与真实资源规则 | 满足后不立即重复，也不永久失去兴趣 |
| 需要目标区间 | 无明确人格设定时先用 [0.45, 0.75]，缺省来源可查 | 安静时能保有兴趣，工作负荷不过度 |
| 时间更新精度 | 解析方案逐维绝对误差 ≤1e-6；采用固定格数值方案则 ≤1e-4 | E01 固定时钟比较 |
| 慢学习 | 每日总绝对变化 ≤0.02，单维绝对偏移 ≤0.15，至少三天独立证据 | 不把短期满足变成角色重写 |
| 自主项目并发 | 1 个运行，最多 2 个持续兴趣 | 私聊响应优先，避免多项目抢设备 |
| 首次空闲复查 | 10min | 运行成本与打扰体验；不是十分钟必做任务 |

采用第 6.4 节的常态涨幅后，以该涨幅反解统一的 rate，不再同时另设一套互相冲突的“满足自然消耗速度”。无法依据人格与体验选择时保留显式缺省。coherence 的整理愿望可以积累，但不凭空累积已接受承诺；competence 的掌握愿望可以增长，但不凭空制造失败；resource_reserve 使用实际负荷与休整记录。

这些参数只能在固定场景回放通过后小幅调整，每次保存参数版本与前后轨迹。首版不让模型在线自由修改驱力增长速率、半衰期与奖励权重，以免用自调参数绕过需要解决的问题。

### 6.4 每项欲望独立的常态涨幅与动态速率

用户追加要求：真正的欲望在未满足时应逐渐上升，且每项都有自己的常态涨幅；亲密／性欲必须保留。采用“人格确定常态参数＋情境有界调制＋实际满足负反馈”，不使用每次随机涨点数。

每个维度配置：

```text
DriveDynamics[d] {
  mode: accumulating | task_derived | resource_derived
  initial_tension, ceiling, reference_tension
  base_growth_per_hour, growth_multiplier_min, growth_multiplier_max
  expression_threshold, high_threshold, high_dwell_hours
  interaction_satisfaction_gain, regulation_relief_gain
  regulation_enabled, recovery_hours, recovery_growth_multiplier
  contact_cooldown_hours, max_contact_attempts_per_window
}
```

`base_growth_per_hour` 明确定义为需求处于参考强度时、无新刺激且不在恢复期的每小时涨幅。例如 `0.02/h` 表示参考强度附近每小时增加约 2 个百分点，不表示任意强度都固定增长 2 个百分点。

设需求强度 `x = tension = 1 - level`、上限 `C`、参考强度 `x_ref`、常态涨幅 `g0`：

```text
k0 = g0 / max(C - x_ref, epsilon)
g_factor = clip(persona_factor * context_factor * recovery_factor, factor_min, factor_max)
k = k0 * g_factor

在本时间段内参数不变时：
x(t + dt_hours) = C - (C - x(t)) * exp(-k * dt_hours)
level(t + dt_hours) = 1 - x(t + dt_hours)
```

这使未满足需求持续有界上升，并保证一次推进两小时与分两次推进一小时一致。情境变化、满足、调节或恢复期结束必须切分时间段，保存参数版本与切换点；不能用新情境系数倒算整段过去。

动态系数允许参考近期活动、关系中明确的互动线索、其他需要竞争和真实负荷。人格已包含在 g0 时 `persona_factor=1`，避免重复加权。冷启动的调制范围先取 `[0.25, 2.0]`，恢复期可使用独立较低下界。模型只能提出有来源的情境标签，由代码映射系数，不直接输出任意增长倍数。

事件激活保留在 activation 层，不能同时又算成提高 x 的同一份刺激。情绪高涨也不能同轮反过来再次提高 g_factor，形成增长—激活的自激循环。

### 6.5 各维度的初始常态涨幅

以下是合成角色回放实验的配置起点，不是人类生理指标或固定人格结论。积累模式初始参考强度设为 `0.50`、上限 `1.00`；具体人格明确指定时覆盖并保留依据。

| 维度 | 参考强度处常态涨幅 | 动态修正与事实区别 |
|---|---|---|
| exploration | 0.040/h | 有未解问题与可改善线索时提高投入倾向；低学习进度提高换方法或暂停概率 |
| relatedness | 0.025/h | 未满足时逐渐想交流；对方没回复不降低信任，也不提高催促频率 |
| intimacy | 0.020/h | 与连接分开积累；明确亲密线索可激活，只在相应关系与互动意愿范围表达 |
| self_direction | 0.030/h | 想做自己的项目；帮助主人也可能满足自主认可，不把所有交办都算压迫 |
| competence | 0.015/h | 增长的是继续掌握能力的愿望；能力水平仍只由实际测试更新 |
| coherence | 0.010/h | 增长的是整理与保持连贯的愿望；具体承诺紧迫度由现存事项另行计算 |
| resource_reserve | 不按纯钟表时间固定增长 | 休整需要由实际负荷上升、真实休整恢复；无负荷不虚构疲惫 |

同一维度只能有一个 homeostatic level 来源。已有具体任务压力、成本和失败证据通过情绪与候选后果参与选择，不能又被无差别加进常态涨幅，导致重复计分。

### 6.6 高位持续、自我调节与恢复期

状态流转：

```text
accumulating → high → regulation_pending → recovering → accumulating
       └──────── 实际满足也可直接进入 recovering ────────┘
```

`x >= high_threshold` 才进入 high，记录 high_since；指数增长不要求恰好到达 1.0。只有连续高于该阈值的时间累计达到 high_dwell_hours，且没有待处理满足或写操作，才产生自我调节候选。跌破阈值就结束本次连续高位计时，不能把不连续的几段任意拼成一次“持续顶点”。

达到高位不是直接调用工具的命令。自我调节仍经过动机—选择—执行—验证；它可以是休整、保存进度、结束当前刺激或一次明确记录的内部调节。默认不对外发送，不自动触发现实身体行为、设备操作或编造亲密活动。

调节完成后：`x_new=max(0, x_before-relief_gain)`，清理本轮高位计时，设置 recovery_until；恢复期把增长系数调低，结束后恢复常态。调节不能提高已验证能力、清空承诺、消除真实分歧或冒记为“用户给予满足”。

亲密／性欲的回放起点：表达阈值 `0.65`、高位阈值 `0.90`、连续高位 `2h`、内部调节缓解量 `0.35`、恢复期 `4h`、恢复期增长系数 `0.25`。这些值可校准；其他欲望按实际调节方式分别配置，不把所有维度写死为同一循环。

恢复期和阈值通过时间边界切分计算。高位调节计时默认只累计程序实际运行的可调节时段；离线可以推算被动需求积累，但不补造多次已经执行的“释放”及其经历。重启后从当前状态决定是否安排调节。

### 6.7 互动满足、内部缓解与表达频率分开

满足事件必须指出维度、实际参与者、关系范围、证据和满足量。普通关心可满足连接需要；只有符合双方互动意愿的相应亲密互动，才作为 intimacy 的互动满足。生成草稿、模型推演、自己说“得到了安抚”不构成用户回应。

互动满足降低 x，并结算一次短期满足；satiety 只用于表达/立即重复的额外冷却，不再次作为第二份相同需要恢复量。内部调节记录 `internal_regulation`，可缓解 x、产生有限的平复感，但不增加对方参与的共同经历，不自动提高关系亲密度或长期欲望。

**内部愈发想要与对外愈发频繁提出是两个独立控制量。** 过表达阈值仅让主动提出成为一个候选，最终仍由联系意愿、情境、待回复状态、联系额度和冷却决定。首次表达后，在对方未回应时不因 x 继续上涨而重复追问；对方拒绝或表示不方便时停止该范围的提出，内部仍可继续积累、调节、恢复，不把拒绝结算成信任扣分。

亲密表达冷却的回放起点为 `12h`，计数窗口从本维度第一次提出时开始，但它只是最小间隔，不是每 12h 自动发一次。全局每日主动联系额度、未回复约束和具体关系边界取更严格者；不能每个欲望各发一次绕过总额度。明确拒绝所建立的表达限制不会因冷却到期自动失效，需按对方后续明确意愿或约定的范围恢复。

### 6.8 配置、结算与回放

欲望各有参数，但统一使用一套按时间更新、按事件结算的实现。参数属于版本化配置，用户不必日常手工管理。以下为拟新增配置结构示例，不是要求立即修改当前 config.toml：

```toml
[life_dynamics.drives.intimacy]
mode = "accumulating"
initial_tension = 0.30
reference_tension = 0.50
ceiling = 1.00
base_growth_per_hour = 0.020
expression_threshold = 0.65
high_threshold = 0.90
high_dwell_hours = 2.0
regulation_enabled = true
regulation_relief_gain = 0.35
recovery_hours = 4.0
recovery_growth_multiplier = 0.25
contact_cooldown_hours = 12.0
max_contact_attempts_per_window = 1
```

内部调节事件与互动满足事件各有独立去重键，共同绑定 root_motivation_id、维度、状态版本与发生时间。记录 `x_before/x_after、phase_before/after、实际满足或缓解量、来源、参数版本`，再原子提交水位与下一复查时间。启动或读取状态不能重复结算调节。

第 6 节 pressure 公式在积累模式下明确使用 `x + learned_offset + activation` 后限幅：人格偏好是增长参数和选择权重，x 是持续需要来源，activation 是额外事件倾向，satiety 是选择/表达阶段的短期重复抑制；不能让同一参数同时扮演增长速度、当前需求和成功奖励三种角色。主循环看到的是整合后的 drive_view，不分别运行两套欲望引擎。

## 7. 学习进度、好奇心与胜任感

### 7.1 不奖励随机噪声

新奇性用于提出问题；是否持续投入要看有无可改善性、真实信息增量或可验证能力进展。网页变化多、输出随机、模型每次回答不同，都不能单独证明值得继续研究。

每个可比较的任务族 `CompetenceState` 存测试定义版本、近期样本、基线误差、当前误差、实际成功/失败、成本、最后独立验证时间。只比较相同测试条件与难度的窗口：

```text
learning_progress = max(0, mean_error(older_window) - mean_error(recent_window))
```

这里用的是“误差下降量”，对应负的时间导数方向；不是直接奖励误差本身。样本不足时标记 unknown，只允许有限探索，不把一个成功称作稳定掌握。知识学习的进展用可核对主张、独立来源与真实应用测量，模型自测仅作理解线索。

### 7.2 胜任感的可实现近似

使用实际任务成功率、目标覆盖、恢复能力和结果稳定性估计 `competence_estimate`，并保留样本量与适用条件。这是工程近似，不冒称已经计算信息论 empowerment。新插件的工具数量增加也不等于控制力增加，必须证明多了哪种可靠可达结果。

没有持续进步且成本上升时，产生换方法、缩小问题或暂停的动机；不要让挫败不断驱动同一重试。网络离线属于恢复等待，不计入“我能力不行”的长期证据。

### 7.3 自主性参数

`autonomy_weight` 调制空闲时自主兴趣与外部可选安排的相对权重，还需保存 `origin、self_endorsed_reason、可替代方案`。它不是心理学自主性的完整测量。

明确接受的用户任务与承诺有独立调度优先级；不能因为今天探索欲高就丢弃。用户没有交办事项时，不必等待外部奖励才开展自己的项目。策略参数变化不得修改权限或无声取消承诺。

帮助主人也可以是她自我认可的目标，不能把所有外部请求都解释成自主性损失。该需要只用于合理安排自己的可支配活动，不产生对用户交办任务的机械抱怨。

## 8. 动机形成与候选选择

### 8.1 从需要到情绪，再到行动倾向

```text
探索缺口 + 旧问题有新线索 → 兴趣、期待 → 想继续研究一个具体问题
真实任务失败 + 存在可改善方法 → 挫败、担心办不好 + 胜任期待 → 想练习/修工具
负荷偏高 + 心境唤醒较低 → 疲惫表达、降低投入意愿 → 保存进度、做小步骤或休整
有意义互动不足 + 关系偏好允许 → 惦记、期待交流 → 想分享；是否联系再独立判断
```

以上是示例，不写成固定任务分派表。需要状态改变时记录一次内在评价；同一个缺口未发生有意义变化，不每分钟制造新焦虑、新经历或新目标。

### 8.2 Want 合约

```text
Want {
  id, origin, scope, semantic_signature, root_motivation_id
  goal, why, expected_outcome, acceptance_checks[]
  source_refs[], affect_cause_ids[], need_ids[], interest_id?
  expected_need_gain{}, expected_affect_shift{}, estimated_cost
  estimated_feasibility, expected_learning_progress, uncertainty
  continuation_value, interruption_cost, expires_at, next_review_at
  status: considered | selected | deferred | dropped | completed
}
```

目标自由生成，执行工具原语保持注册与可验证。主观期待是预测字段，不能写入实际结果；模型不能自己生成已授权 token 或声明测试已经通过。

### 8.3 选择顺序

先检查用户打断、已接受承诺、授权、来源、当前资源和重复任务；再对可行的自主候选评分。无可行候选可以保持安静。

```text
motivation(w) = affect_fit(current_affect, predicted_valid_outcomes(w), persona)
score(w) = autonomy_weight * motivation(w)
         + continuation_bonus + bounded_learning_progress_bonus
         - effort_cost - interruption_cost - repeated_failure_cost
         - immediate_repeat_penalty - unnecessary_contact_cost
```

`affect_fit` 使用期待、兴趣、担忧、挫败缓解等有对象的行动倾向，结合候选能否真实影响这些原因。基础需要已经进入内在评价，不再把同一 deficit 以大权重重复计分。学习奖励上限固定，不能用夸大的“预计进步”压过已知成本。

预测收益需按实际历史校准；新目标采用保守先验与有限探索额度。最高分未超过启动门槛则安静。已有项目继续获得惯性；只有新候选比当前项目高出切换门槛，或当前项目已不适合，才切换。初始最多一个运行的自主项目、两个保留兴趣，可调但不是每日固定轮转菜单。

实现 `affect_fit` 时，不留下一个未定义黑箱：

```text
match[k,w] = 该方案对情绪 k 的具体原因/兴趣是否相关，归一化到 [0,1]
expected_gain[w] = 按任务族实际结果校准后的预期需要改善，归一化到 [0,1]

approach[w] = beta_interest * anticipation * match[anticipation,w]
            + beta_joy * joy * match[joy,w]
            + beta_expectation * expected_gain[w]

repair[w] = sum_k(beta[k] * E[k] * match[k,w] * feasible_relief[k,w])
            # k ∈ {concern, frustration, hurt}

affect_fit[w] = clamp01(approach[w] + repair[w])
```

`match` 必须绑定实际 cause/interest ID；澄清真实误会、修复实际故障可形成 relief，销毁记录或伪造成功不可形成。`expected_gain` 来自同一内在评价产生的候选后果，只计入一次；它允许情绪平稳时仍因正面期待而开始活动。

初始 beta 总和归一化为 1；在无明确人格偏好时平均分配并标为缺省。成本、继续奖励和切换门槛同样归一化，由第 18 节固定候选测试校准。模型的 match/收益预测在冷启动时最多按 0.5 可信度缩减，后续由独立结果校准，不把自报 confidence 当成功概率。

ChoiceRecord 必须记录每一项分数、硬性排除原因、候选引用、参数版本和落选理由。“她今天为什么继续研究而没有联系你”能够从记录解释，但普通回复不逐项朗读这些分数。

### 8.4 唤醒节奏

使用现有 WorkQueue 安排 `motivation_review`。以下事件可唤醒：新证据、实际结果、明确期限、资源释放、空闲窗口开始、兴趣复查到期。没有变化只推进纯时间视图，并退避到下一复查点，不持续调用模型想新念头。

待调起点：用户最后一次有效互动后 10 分钟可进行首次空闲复查；有项目时按项目下一步排期，无项目且无新证据时逐步退避。阈值是低成本配置，不在模型提示词里散落常数，也不要求用户日常填写。

## 9. 完整主循环与事务边界

### 9.1 主伪代码

以下为总入口形态，方法名为拟新增接口，不是声称当前已有。完整处理函数见配套文档 P14–P15；入站回复、自主复查与执行作业分别分派，避免每次收到消息都顺带启动一轮自主工作：

```python
async def life_cycle(job):
    # 租约、epoch 和来源在每个 handler 的提交/执行边界校验。
    if job.kind == "respond":
        return await on_user_message(job)       # 认知→情绪/关系提交→回复
    if job.kind == "motivation_review":
        return await review_motivation(job)     # 动机→选择→持久计划
    if job.kind == "plan_step":
        return await execute_one_step(job)      # 执行→验证→满足/检查点
    return await dispatch_job(job)              # 时间、学习、对账、发布观察等
```

每次模型或工具等待后重校验版本和 epoch。实际外部结果晚到时保留必要回执，但失效上下文不再提交正文、满足或下一步行动。一次已发生动作的 unknown 先对账，不重新让模型规划并重复做。

### 9.2 原子提交的四组内容

1. 输入事件、轮次与持久作业。
2. Appraisal、贡献记录、状态快照、效果去重和时间水位。
3. 调用 intent/dispatching 记录，先于外部副作用；结果落盘后再派生状态。
4. 验证结果、经历记忆、满足结算、能力样本和任务检查点。

不跨模型/网络等待持有 SQLite 写事务。跨进程不假设存在一个分布式事务，依赖可查询调用 ID、状态日志和幂等恢复。

### 9.3 用户回来时

入站消息优先处理。自主任务在下一安全边界暂停；支持取消的只读请求可取消，插件安装与文件切换等已开始写操作先收束到明确状态。回复可以先进行，不等待整个自主项目结束；设备与生产切换仍由资源租约串行。

没有真实进展就不编造近况。已经做过的事情即使当时没分享，也可以在相关聊天时自然提起。恢复自主任务前检查用户是否取消、目标是否仍有效、资料和权限是否过期。

## 10. 执行结果怎样变成记忆、满足和下一次选择

| 实际结果 | 记忆 | 满足/情绪 | 后续选择 |
|---|---|---|---|
| 学到有来源的新内容 | 知识主张＋来源＋适用条件 | 有限理解满足与期待 | 缩小未解问题，寻找应用，不重复读同页刷满足 |
| 插件安装完成但工具未出现 | 安装记录，能力仍未就绪 | 不结算胜任满足 | 挂载/重启/发现工具或诊断桥接 |
| 工具出现且独立业务验证通过 | 能力版本＋调用证据 | 一次成就感与胜任满足 | 原任务仍有效时续接 |
| 自改测试通过但无独立收益证据 | 候选与对比结果 | 可有完成实验的局部满足，不宣称升级成功 | inconclusive，保留问题与下一证据需求 |
| 实际目标完成 | 完成经历、实际结果、对话回执 | 按根经历结算满足 | 关闭任务，抑制立即重复 |
| 网络错误 | 暂时故障与恢复点 | 有界挫败；不长期否定能力 | 退避，换其他可行活动 |
| 结果不明或调用中失联 | unknown 与外部 operation_id | 不结算目标满足 | 对账，暂停同一副作用重试 |

“愉快”与“正确”分开：成功的感受不会把冲突来源变成真；对话中的亲密表达不等于已经完成现实行为。

## 11. DSH 本地事实与桥接方式

### 11.1 本轮核对到的实际接口

| 本地路径/符号 | 已有能力或限制 |
|---|---|
| `D:\projects\dsh\DeepSeek Harness.exe` | 找到桌面软件；不把 Electron 程序当作可直接执行 `plugin` 的 CLI |
| `D:\projects\deepseek-harness-master\deepseek-harness-master` | 找到源码；根 package.json 为 `0.2.1-rc.2`，不能据此认定已安装运行时相同版本 |
| `apps/cli/src/plugin.ts:runPlugin` | 已有 `dsh plugin --profile <name> add/remove ...`，内部共用插件管理操作 |
| `packages/boot/plugin-manager/src/tools.ts` | `plugin_manager` 提供 list_plugins、list_bundles、set_plugin、set_bundle、install_bundle、remove_bundle 等 |
| `packages/boot/plugin-market/src/tools.ts` | `plugin_market` 提供已配置市场的索引、搜索与版本列表；不由模型任意增加市场源 |
| `packages/core/tools/src/index.ts` | `ToolRuntime.schemas(scope)`、`get(name, scope)`、`execute(exec)`；执行含前置/后置策略、取消等管道 |
| `packages/sdk/protocol/src/types.ts` | SDK 已有 initialize、session/prompt、resume、wait、工作目录和 shutdown；**没有 tools/list 或 tools/call** |
| `python/sdk/src/deepseek_harness/client.py` | HarnessClient 可复用 stdio JSON-RPC；必须显式 DSH_HOME；`dsh_bin` 是 CLI，不是桌面 EXE |
| `packages/interaction/user-approval/src/index.ts` | 请求审批必须发生在有效 open turn 内；仅构造一个 session 然后裸调工具不够 |

旧 `evo/dsh.py` 和技能文档中“装完必须自己加 bundles 且一律重启”的描述需要按当前管理器更新：管理器会处理组合包选择；有 HMR 的变更可能立即生效，也可能 restart-required。不能把所有成功都等同于热加载成功。

### 11.2 采用一条桥，保留一个认知决策者

```text
Bot / LifeRuntime
  └─ DshBridge（Python 异步适配；复用官方 HarnessClient）
       └─ 受管理的本机 dsh --profile sdk 进程
            ├─ 扩展 SDK JSON-RPC：qq_life/*（已实现 packages/sdk/server/src/server.ts:401、:618）
            ├─ qq-life-bridge 插件：会话、调用记录、授权与取消
            ├─ DSH tools 管道
            └─ plugin_manager / plugin_market / read / edit / write / pwsh / web_*
```

常规调用无需再让 DSH 大模型重新决定“她想做什么”。QQ 的认知引擎生成步骤，桥把确定的工具名与参数交给 DSH 执行。DSH Agent/Session 用于作用域、日志、策略和资源生命周期，不构成第二个人格。

第一版桥要同时修改 DSH SDK 路由与新增业务插件：SDK server 只负责转发 `qq_life/*` 至注入的服务，插件拥有业务规则。不能拿现成 SDK 假装已支持任意工具 RPC，也不能另起第二个 stdin 读取器争抢协议。

### 11.3 运行位置与启动

使用本机正常进程。为 bot 设置 `D:\projects\qq-deepseek\data\dsh-home`，使用其中的 `profiles/sdk`，与用户桌面 DSH 的 profile 分开，目的是管理归属与恢复清晰；这不是操作系统沙箱。

启动器寻找或构建与已核对源码一致的 DSH CLI/runtime，记录 CLI 路径、运行时版本、构建 hash、SDK 版本。不得把 `D:\projects\dsh\version` 中的 Electron 版本当作 DSH 协议版本。源码启动遵循该仓库的 `pnpm dsh --profile ...`；打包后走受支持 CLI，不能用包内部 bin 或 SDK 私有 `_launch_args` 作为生产接口。

初始化流程由原启动入口自动完成：发现 CLI → 准备专用 home 的 sdk profile → 安装固定版本桥 bundle → 加载管理/文件/终端/网络工具 → 握手核对 schema。桥不可用时私聊仍可进行，自主执行作业等待恢复并提供诊断，不自动退回任意 shell 拼接。

下面是未来实现的 SDK 使用形态；`qq_life/*` 当前需要先实现，不能直接复制当成已能运行：

```python
client = HarnessClient(HarnessConfig(
    dsh_bin=resolved_cli,
    profile="sdk",
    dsh_home=str(root / "data/dsh-home"),
    cwd=str(root),
    request_timeout_seconds=bounded_timeout,
))
client.start()
client.initialize(cwd=str(root), provider=provider, model=model)
hello = client.request("qq_life/hello", {"protocol": 1})
assert hello["protocol"] == 1
tools = client.request("qq_life/tools/list", {"executionScope": scope_binding})
```

SDK 初始化会解析配置的模型路由；直接工具调用不需要新增模型推理，也不发送 persona 全文。SDK 客户端继承环境的行为需在启动适配中明确控制；凭据只交给确有需要的 DSH 服务，不能夹在工具参数、日志或向模型展示的结果中。

专用 sdk profile 的最小组成从源码的 `PROFILE_TEMPLATES.sdk` 起步（`@deepseek-ai/dsh-base` 与 `@deepseek-ai/dsh-sdk-app`），再安装本项目 bridge bundle 和经清单确认的管理/业务 bundle。应先 dump 合成配置检查实际缺项；不能假定安装了 SDK 就一定已有 plugin_manager、HMR 或 web 工具。桥 bundle 的 `dsh.bundle`、依赖与 patch 由实施时的真实组合测试确认。

## 12. 工具桥协议与完整执行管道

### 12.1 新增协议，不伪装成上游现有 API

| 新方法 | 用途 |
|---|---|
| `qq_life/hello` | 协议、运行时、profile、工具 generation 与能力握手 |
| `qq_life/tools/list` | 当前执行作用域真正可见的 schema，支持分页与按任务筛选 |
| `qq_life/execute` | 执行一个确定工具调用，先持久化 operation_id，再进入管道 |
| `qq_life/status` | 按 operation_id 查询已落盘结果与副作用状态 |
| `qq_life/cancel` | 取消指定调用，返回最终收束状态，不假装能撤销已完成副作用 |

结果通知复用同一 stdio 通道，带 operation_id、调用 generation 和阶段。大输出、截图和文件返回有界摘要及 artifact 引用，读取 artifact 仍受任务归属与大小限制。

```json
{
  "jsonrpc": "2.0",
  "id": "rpc-17",
  "method": "qq_life/execute",
  "params": {
    "operationId": "op-unique",
    "executionScope": "bound-scope-id",
    "planId": "plan-unique",
    "stepId": "step-3",
    "toolGeneration": 8,
    "tool": "plugin_manager",
    "arguments": {"action": "install_bundle", "target": "<exact-spec-from-catalog>"},
    "grantRef": "grant-bound-to-exact-operation",
    "forgetEpoch": 4,
    "policyEpoch": 2,
    "deadlineMs": 120000
  }
}
```

`grantRef`、executionScope 和 epoch 由宿主生成并绑定，不由模型填造。DSL/协议不接受任意 Python/JS 函数字符串作为路由。所有 target 从实际工具结果或已绑定项目资源取得，包名不猜测。

桥 bundle 的可安装载荷结构明确如下，名字为本项目拟新增包名，不是假设市场上已有该包：

```text
integrations/dsh/qq-life-bridge/
  package.json                  # ESM，dsh.bundle.patch 指向下面的 patch
  cordis.patch.yml              # 挂载 qqLifeBridge 服务及工具前置授权适配
  src/index.ts                  # 服务导出及生命周期
  src/protocol.ts               # qq_life/* 请求/结果校验
  src/execution.ts              # Agent/turn/工具管道接线
  src/operations.ts             # 调用落盘、查询和取消
  lib/                          # 与目标 DSH 版本一起构建的运行产物
  tests/                        # Loader 真实组合、工具调用、取消与卸载测试
```

manifest 使用已核对的 `"dsh": {"bundle": {"patch": "./cordis.patch.yml"}}` 格式。patch 的真实父行与模块路径以目标 profile 的 dump-config 为依据；不猜写 entryId。安装来源使用这个包的版本化本地 artifact，之后仍由 plugin_manager 管理。

### 12.2 Python 与 DSH 两侧的职责

Python：检查作业租约、源事件、作用域、授权、预算；持久化调用意图；通过 `asyncio.to_thread` 调用同步 SDK；关联 late result；验证目标；事务结算。

DSH：握手确定 profile 身份；校验 wire JSON 与绑定；持久化同一 operationId 的参数 hash；查找该 Agent 范围内真实 schema；通过 `ToolRuntime.execute` 执行；持久化完整结果；响应查询和取消。

`grantRef` 不能只因为字符串存在就被信任。DSH 在派发前经同一双向通道向 Python 发出新增请求 `qq_life/authorize`，包含 operationId、规范化参数 hash、实际工具和资源、scope、plan/step 及 epoch。Python 根据本地已落盘作业与 grant 作当前校验，仅对精确调用返回 allow/deny/waiting，不能由这条回调创建扩大范围的授权。

Python 必须有独立的 SDK 请求泵，使用现有 `next_request / respond / respond_error`，在等待 execute 响应时仍处理授权和审批请求，避免双向调用死锁。任何未知回调 fail closed 并记录诊断。只接受自己启动并握手的 stdio 子进程；不把协议暴露成没有身份校验的局域网接口。

### 12.3 必须补齐 open turn 与日志

新增 BridgeExecution 服务为每个执行作用域持有 DSH AgentHandle，使用 Agent 对应 Session 和 cwd。一次桥调用在该 Agent 空闲时取得执行所有权，建立合法 turn/step，按当前 Session 类型提交 `tool/call`，执行管道，再提交关联 `tool/result` 与终止事件。异常也必须闭合或由重启修复判为中断。

复用源码 `agent-loop/src/tool-calls.ts` 的日志约定；需要抽取共享执行入口时，同时更新普通 Agent consumer，避免两套不一致的事件格式。`tools/result` 通知本身不代替 Session durable log。审批位于 open turn 中，经桥通知到原有自然确认入口；不能在 idle session 裸调 `approval.request()`。

服务注册工具必须保留 Agent scope；`tools.execute` 必须传 `agent、callId、arguments、signal`。桥 profile 使用 `mode: native`，按实际 schema 直调；不能伪造 PTC parent token 绕过仅允许 run_code 的模式限制。

SDK 的通用 `session/prompt` 不得与桥的同一 Agent 同时占用一个 turn。执行器取得该 Agent 的执行所有权后，严格按顺序开/闭 turn；涉及新增 Agent 执行生命周期入口时，必须在 DSH agent 层提供支持并同时验证状态、取消、持久化与 whenIdle，不能只手工 append 几条事件伪装 Agent 已处于运行中。

具体入口是已实现的 `runExternalToolTurn(agent, call, signal)`（packages/core/agent-loop/src/agent.ts:218，SDK 侧调用点 packages/sdk/server/src/server.ts:502）：由 agent-loop 拥有一次完整执行区间，取得 Agent 执行所有权、转 running、创建 turn/step、提交 call、执行工具管道、提交 result、闭合 step/turn，并在清理完成后释放所有权、转 idle。复用普通 loop 的日志函数及取消/错误归一化，外部 consumer 不访问私有 SessionRecord 或伪造状态标志。这个入口是首个桥里程碑的交付项，现已落地并真机跑通；只连上 JSON-RPC 仍不算工具桥完成，验收看执行落盘、取消与断线对账。

### 12.4 授权复用与异常状态

已有合法授权按计划、效果、资源、期限和当前 policy epoch 复用，不逐工具重复问。DSH 侧的 full-access/approval 规则仍须与该专用 profile 明确配置一致；普通安装许可不自动批准未知构建脚本、版本豁免或改变目的地。DSH 管理器原本就要求特殊审批的分支，桥原样返回待确认信息，并落入现有聊天确认方式。

维护动作、启动模板、预算等由部署配置一次确定，不变成用户每天填写的表单。软件使用本机权限；应用层资源绑定不等于能限制恶意本机插件访问整个文件系统。

### 12.5 恢复与幂等

DSH 调用记录落在专用 home 中，Python 调用记录在 life 库中。相同 operationId＋相同参数 hash 返回同一已知结果；相同 ID 换参数直接拒绝。DSH 接受记录先于副作用。

Python 超时后先 `qq_life/status`。DSH 重启发现 dispatching：只读操作可按既定策略重新读取；包安装、删除、文件写入和发布操作标 unknown，并依据磁盘与工具清单对账。不能把网络请求 ID 当成具备 exactly-once 的外部事务保证。

取消进入 stopping 时保留资源租约，直到旧动作完成、整棵进程树退出或明确已收束；不能先释放设备/profile 锁再等待旧调用。旧 generation 的结果不激活新工具或续接任务。

## 13. DSH 插件发现、添加、启停、删除和升级

### 13.1 发现与添加

优先调用 `plugin_manager list_bundles` 获取官方可装目标；已配置自定义市场则用 `plugin_market list_indexes → list_plugins → list_versions`。取具体 install spec 与 registry，保存来源和版本，不用模型猜包名或任意追加市场源。

```text
能力缺口与业务验收
  → 获取确切 bundle/spec
  → 检查版本、已有安装和所需效果
  → plugin_manager.install_bundle
  → 解释 ChangeResult
  → 如需重启，保存检查点并只重启 bot 所属 DSH 进程
  → tools/list 重新发现
  → 工具 schema/作用域匹配
  → 独立业务调用验证
  → capability.status = active
  → 原任务仍有效时继续
```

`ChangeResult.application` 必须完整映射：

| DSH 返回 | 本地状态 |
|---|---|
| applied | DSH 认为改动已应用；仍待工具发现与业务验证 |
| restart-required | 等待受管理重启；保存 restartReason，不假装已经可调用 |
| overridden | 已保存但被更高层覆盖，保持未激活并诊断 |
| failed | 保存阶段与错误、实际残留；不结算成功 |
| cancelled | 安装已取消，核对实际恢复结果 |

`pendingBuilds` 需要原有授权语义明确覆盖后才传 `approvedBuilds`；`incompatible-version` 不自动用 acceptRisk 放行。可以换兼容版本继续，而不是每次失败都要求用户介入。

### 13.2 启用与停用

`set_bundle(target, enabled)` 管理组合包选择；`set_plugin(entryId, enabled)` 管理准确条目。entryId 必须来自 list_plugins。关闭业务插件后观察工具 schema 消失；桥缓存随 tools/change 或重新握手失效。

桥本身、审批、Session 存储、调用记录和插件管理的必要组件不能被自主维护关闭。当前管理器已有 management-required 等拒绝，桥不能绕过直接改 patch。

### 13.3 删除

先找引用该插件的待执行任务与正在运行调用；选择等待或暂停依赖任务，再对精确 bundle 调用 remove_bundle。不能只删除 Caps 清单后声称已卸载 DSH 插件。

移除成功后重新读 list_bundles 与 tools/list，验证依赖/选择/活跃工具状态。`bundle-in-use`、`not-removable`、`stop-profile` 按结构化诊断处理。缺少 HMR 且必须停机时：保存所有调用 → 等待 DSH 安全退出 → 用相同 CLI 和 DSH_HOME 执行 `dsh plugin --profile sdk remove <exact-bundle>` → 重启握手。

CLI 的标准 add/remove 可用于初始化和停机维护；不通过直接 `pnpm add` 绕开 DSH 的组合包管理。源码启动命令的前缀与打包 CLI 的前缀由安装适配器确定，参数数组中不拼 shell 管道。

### 13.4 升级与回退

先记录旧 bundle 精确版本、来源、lockfile 摘要、相关 patch、业务探针与依赖关系。当前 HMR 管理器对已装版本可能返回 already-installed，需要先卸载再装目标版本；这是有停用窗口的维护操作，不是无缝原子升级。

桥持有 profile 维护租约，暂停依赖调用；按管理器支持顺序完成升级，重发现工具并跑相同业务探针。失败时通过 DSH 正常管理操作恢复旧版本，重新验证；恢复也失败则保持 unavailable，等待诊断。不能声称管理器会自动回退一切激活故障，也不能在运行中仅恢复 manifest 而忽略已加载模块。

DSH 插件可能写自己的数据；恢复代码/依赖不等于回退插件业务数据。没有明确数据迁移/降级协议的版本，不承诺可自动无损降级。

## 14. 通过 DSH 现有工具修改自己

支持两个明确目标：改 QQ-DeepSeek 的代码；改 bot 所属 DSH profile 的自定义插件。改变核心 DSH 桥或管理基础设施由维护发布处理，不能让候选自改自己的验证控制器。

源码已核对工具：`read(file_path, offset, limit)`、`edit(file_path, old_string, new_string, replace_all)`、`write`；Windows 终端工具名为 `pwsh`，另有 `web_search` 与 `web_fetch`。除给出的 read/edit 示例外，其他参数以当前 tools/list 为准，不臆造通用 shell/run 参数。

```text
实际失败证据 → 自修动机 → 可验证改进提案
  → ReleaseController 准备本机候选代码目录
  → DSH Agent cwd 绑定候选目录
  → read 查代码 → edit/write 修改 → pwsh 执行受控测试
  → 独立发布器检查 diff、基线、固定验收、成本与回归
  → 证据充分才切换；观察不足保持 evidence_pending
  → 发布器停止旧 worker 后替换代码并重启、观察、必要时恢复
  → 记录真实改进经历，按原任务状态决定续接
```

候选目录在同一台电脑、同一正常运行环境下工作，不要求 Docker。DSH 给出的“测试都通过”只是工具输出；独立控制器自己读取退出状态、完整对比和业务结果，不接受候选替换固定测试。人格文件、密钥、记忆与删除标记不随代码发布回滚。

修改 DSH 插件源码时：在本地源目录制作版本化 bundle，检查 `package.json` 的 `dsh.bundle` 和实际工具声明；通过 DSH 管理器安装该本地版本，重新发现、验证，再更新能力记录。不直接编辑共享 `node_modules` 后假装升级可复现。

自修失败产生的挫败只支持有限重规划，根缺口共享实验额度；换提案 ID 或换插件名不能重置同一根问题的预算。

## 15. 数据存储与代码改造清单

### 15.1 复用现有状态与表

`state_snapshots` 新增带 schema_version 的 homeostasis、drive_v2、competence 载荷；`state_journal` 保留录制 Appraisal、输入版本、时钟和完整变化。wants.payload 扩充第 8 节合约；interests.payload 保存问题、进展与下一步。继续复用 work_jobs、resource_leases、budget_ledger、grants、deliveries、open_loops、knowledge_cards 和发布器 journal。

优先只新增三个有实际消费者的持久记录：

1. `affect_contributions`：情绪/心境/关系贡献与撤回索引，绑定 root/appraisal/作用域。
2. `drive_settlements`：激活、满足、慢学习的有符号贡献，带根经历和唯一结算键；可按来源重建。
3. `bridge_operations`：operation_id、请求 hash、归属、工具版本、阶段、结果引用和对账状态；Python 与 DSH 各保存本侧记录，以 ID 关联。

不为每个情绪词、每个模型念头或每个工具再建一个服务。原因和结算记录中的可读正文继承来源删除策略；恢复旧备份前先应用现有独立删除账本。

### 15.2 文件责任

| 文件/区域 | 具体改造 |
|---|---|
| `life/persona.py` | v2→v3 参数映射、来源、局部编译、版本迁移，不重置关系 |
| `life/state.py`、`contracts.py` | 内稳态、驱力、精确时间更新、有符号贡献、心境与满足规则 |
| `life/appraisal.py` | 结构化语义评价、对象/来源/澄清校验、internal_state 用途校验 |
| `life/runtime.py`、`replay.py` | 统一状态提交、时间水位、贡献重建、只读 view_at |
| `life/attention.py`、`planning.py` | 自由 Want 生成接线、可行性与惯性、完成抑制、兴趣续接 |
| `life/work_queue.py` | 条件唤醒、quantum、退避、用户打断和已开始动作的收束 |
| 拟新增 `life/dsh_bridge.py` | Python SDK 适配、schema 缓存、调用/取消/恢复；不存情绪业务规则 |
| `evo/dsh.py`、`evo/tools.py` | 原有 dsh 操作转发到一条桥，删除“依赖安装=激活”的假成功判定 |
| `life/bridge.py`、`evo/caps.py` | DSH bundle 清单与可调用工具映射；Python pip 能力作为另一个明确 backend |
| `life/release.py`、`supervisor.py` | DSH 候选编辑产物对接、独立验收、代码切换、观察结果反馈 |
| `bot.py` | 入站接线、认知调用、自然确认、状态与进度摘要；不继续堆叠动力学实现 |
| 拟新增 `integrations/dsh/qq-life-bridge/` | 可安装 DSH bundle，拥有桥业务、Agent turn 生命周期与调用记录 |
| DSH `packages/sdk/server` 与 `protocol` | 版本化 `qq_life/*` 扩展路由/合约，与普通 SDK 调用兼容；业务交给插件服务 |
| DSH `packages/core/agent-loop` 与 Agent 生命周期入口 | 新增 runExternalToolTurn，复用 call/result 日志，统一外部工具 turn 的执行所有权、状态、取消、收束与 whenIdle |
| DSH `python/sdk` | 保留现有公共 SDK 形态，补桥类型适配与双向请求处理；如启动器需要进程所有权钩子，增加公开受支持选项，不依赖私有 _launch_args |
| `skills/dsh*.md` | 按实际管理器、桥、业务验证更新文档，不保留过时 pnpm 手工挂载说明 |

只有实施涉及 DSH 源码时才按其 AGENTS、构建和真实组合测试要求修改；本次仅编写设计文档。

## 16. 完整行为示例

### 16.1 你没找她，她自己把一个方法研究明白

最近一次真实任务显示网页处理方法不可靠 → 形成有对象的挫败和“想掌握它”的期待 → 空闲窗口评估探索/胜任缺口 → 生成“比较两种解析方法，找到可复现原因” → 选择一个小实验 → 经 DSH 查询资料、读代码、跑样例 → 用固定样例核对 → 保存结论与来源 → 结算一次胜任满足，降低重复压力 → 原问题未全解决则保存下一步。

没有必要在完成每一步时发消息。你回来问她最近在做什么，她可以根据这段真实经历回答。没做成就说还卡在哪里。

### 16.2 她需要插件才能继续

具体问题缺少工具 → 欲望高不直接触发安装 → 提出带业务验收的能力改进目标 → 查 DSH 市场和已装 bundles → 通过 plugin_manager 安装 → restart-required 时保持 waiting → 受管理重启 → schema 出现 → 业务验证成功 → 胜任感有依据地改善 → 原任务仍有效才继续。

若工具不存在、业务验证失败或原任务已取消，不说“已经进化好了”，不向联系人自动演示。

### 16.3 你说想安静待着

认知判断 quiet_company → 保存相处偏好与当轮需要 → 降低打扰倾向与追问密度 → 可以安静回应，也可以暂停无关报告 → 之后空闲时做自己的事情。连接需要不会因此变成无限上升的委屈；你再次开口时自然接上。

### 16.4 连续失败但没有学习进展

同一探针反复失败，误差不下降 → 有限挫败＋能力估计暂降 → 重复失败成本提高 → 换更小问题/不同方法/暂停 → 保留否证记录。不会靠重装同一插件或重读同一网页反复领取新奇与满足。

## 17. 迁移与实施顺序

1. **状态数学先修。** 统一单位与时间推进，分离十四维人格和七类基础需要，明确保留亲密／性欲维度；实现各维常态涨幅、动态速率、高位与恢复，修正符号、满足学习串线、去重与积分。以录制事件双算新旧状态，不同时影响实际行为。
2. **情绪语义与修复。** 让私聊 Appraisal、对象归因、quiet_company、原因撤回与关系贡献进入统一提交。修复先于更激进的自主动作。
3. **闭环的小规模运行。** 在既有工具上打通一个持续兴趣与一个真实秘书任务，验证选择、执行、结果、满足和继续，不能只做 Want 服务接口。
4. **DSH 桥与插件生命周期。** 完成真实 profile 组合、SDK 扩展、工具发现、添加/删除/升级和业务验证；旧 DSH 路径统一转发。
5. **能力发展与自修改。** DSH 修改本机候选、独立验收、发布观察、失败回退、原任务续接。
6. **连续体验验收。** 用固定时钟、录制模型输出和真实工具夹具进行跨天场景；线上验证在确认运行配置后开展，结果与离线测试分别记录。

迁移不把旧“嫉妒 8/10”直接转成“社交缺口 0.8”。稳定设定按人格依据迁移，已有有效情绪按通道与来源迁移；无法解释的欲望临时量保留只读旧快照并从中性当前值开始。学习偏移必须有独立证据，否则不继承为已证明偏好。

已有历史、有效承诺、好友关系和删除记录保留。新旧实现仅一个负责状态写入；影子比较不触发真实 DSH 副作用。业务启动方式保持不变。

## 18. 验收矩阵

这些是新增实现的退出条件，与初版 T01–T62 并行使用，不把测试名称存在算作通过。

| 编号 | 场景 | 必须成立 |
|---|---|---|
| E01 | 一小时一次更新 vs 60 次更新 | 情绪、心境、需要与精力在约定容差内一致；不因 tick 数饱和 |
| E02 | 时间回拨、跨午夜、离线两天 | 水位不倒退、不重复积分与额度结算、不编造活动 |
| E03 | 负向 correction | 对应激活下降或保持，不反向增加；无关维度不改变 |
| E04 | 一次真实满足后当日慢结算 | 短期压力下降；没有独立慢学习证据时 learned_offset 不变 |
| E05 | 同根事件涉及探索与胜任两维 | 各合法效果一次，复述/日记/重启均不再结算 |
| E06 | 同样内容一条消息 vs 十条碎片 | 语义经历接近，长期关系不按条数机械增长 |
| E07 | “同事说滚”“操作硬盘”“我很累想安静” | 对象与用户需要正确，不靠单字误伤关系 |
| E08 | 澄清旧误会 vs 对真实冲突道歉 | 前者撤回无依据贡献，后者保留经历并局部修复 |
| E09 | 查看状态/检索一百次 | 无新情绪、关系、满足和长期学习 |
| E10 | 没有用户新指令但有持续兴趣 | 动机可从人格/内稳态/旧问题产生，执行有依据且不伪造用户指令 |
| E11 | 没有合适目标或资源不足 | 可以休整和安静，不为填满循环编任务 |
| E12 | 用户回来/取消原任务 | 回应优先、动作安全收束、检查点可恢复；取消目标不自动续做 |
| E13 | 随机噪声 vs 可改善问题 | 有限探索后更偏向可验证进展；不因随机性永久占用资源 |
| E14 | 改人格一个字段/重启 | 仅相应参数迁移，经历、关系与有效承诺保留 |
| E15 | 同人格参数变化但相同候选 | 仲裁与节奏出现可解释差异，权限与事实结论不变 |
| E16 | 用户情绪低落但没有针对她的负面事件 | 正确估计倾听/陪伴需求，不把用户的烦躁直接复制成她对用户的不满 |
| E17 | 当前情绪平稳，探索需要有合理缺口 | 可因兴趣与正面期待开始研究，无需先制造痛苦 |
| E18 | 自我调节候选包括“删失败日志” | 不作为有效缓解与成功；优先真实休整、换方法或修复问题 |
| E19 | 同时让各积累型欲望处于相同参考强度 | 按各自常态涨幅增长；亲密／性欲有独立状态，不并入普通连接 |
| E20 | 情境系数中途改变，或跨入恢复期 | 按实际时间边界分段更新；一次长推进与多次短推进在容差内一致 |
| E21 | 亲密需求连续高位达到时限 | 产生一次内部调节候选，完成后缓解并进入恢复期；不冒记用户参与 |
| E22 | 亲密需求高位期间重启或重复读取 | 高位与恢复时间保存，不重复释放或结算；不补造离线活动 |
| E23 | 主动提出后用户未回复、拒绝或要求安静 | 内部可继续变化，但不升级追问、不扣信任；遵守关系与联系边界 |
| E24 | 同一互动既恢复 level 又生成 satiety | 不重复领取同一份满足，不因此直接增大长期欲望偏移 |
| B01 | 普通 SDK 连接未扩展 DSH | 明确报告桥协议缺失，不假装 tools/call 可用 |
| B02 | 真实 dsh profile 挂载桥 bundle | 握手和 schemas 可用；无模型调用也能完成一个 read 探针 |
| B03 | DSH 管理器要求审批 | 审批发生在合法 turn 内，经现有确认入口返回，日志可重放 |
| B04 | install applied / restart-required / overridden | 三种状态分别处理，只有业务调用验证后 active |
| B05 | 删除精确 bundle | 依赖任务处理、工具消失、清单核对，不只删 Caps JSON |
| B06 | 插件升级失败/旧版本恢复失败 | 正确暴露停用窗口、回退结果和 unavailable，不假装无缝成功 |
| B07 | 同 operationId 重试/参数变更/执行后丢回执 | 已知结果复用、换参数拒绝、未知副作用先对账 |
| B08 | tools generation 变化或 epoch 失效 | 陈旧 schema/结果不执行后续动作，不结算满足 |
| B09 | 用 DSH edit 修改自身候选 | 生产源码不提前切换；独立验收失败不发布；不改 persona |
| B10 | 跨天学习→能力验证→原任务续接 | 同一根问题与进度贯通；原任务取消则不续做 |
| D01 | 删除来源时模型/DSH 调用仍未返回 | 派生状态、长期贡献、缓存与待办失效，旧结果不能复活 |
| D02 | 回放相同事件/模型输出/时钟/规则 | 状态与选择可比较复现，不执行任何真实副作用 |
| X01 | 主人私聊与少量好友长期交错 | 各自经历和关系正确；自己的可分享活动不泄露私人动机 |
| X02 | 连续一周录制轨迹与人工私聊评审 | 观察兴趣持续、误会修复、任务兑现、打扰次数与实际学习进展，不追求最大忙碌率 |

## 19. 研究依据与工程取舍

本设计采用“情绪参与驱动与仲裁”的合成代理架构，参考 [Velásquez 1997：Modeling Emotions and Other Motivations in Synthetic Agents](https://cdn.aaai.org/AAAI/1997/AAAI97-002.pdf)。这是一种可实现的设计路线，不把“所有行为只能通过某一种情绪机制产生”当作已被证明的普遍定律。

学习进度与知识/胜任驱动的区别参考 [Oudeyer 与 Kaplan 2007 的计算内在动机分类](https://www.frontiersin.org/journals/neurorobotics/articles/10.3389/neuro.12.006.2007/full)，自生成目标参考 [Forestier 等的 IMGEP 论文](https://arxiv.org/abs/1708.02190)。本文的目标族、测试窗口和评分公式为本项目工程方案，不是论文原公式的直接复刻。

自主、胜任与关系需求参考 [Ryan 与 Deci 2000 的自我决定理论论文](https://www.selfdeterminationtheory.org/SDT/documents/2000_RyanDeci_SDT.pdf)。自主性在该理论中涉及意愿与自我认可，本文的 autonomy_weight 只是调度参数，需结合动机来源和自我认可理由，不能拿一个比例声称测得了完整自主性。

严格的 empowerment 定义涉及动作到后继状态通道的信息容量，参考 [Klyubin、Polani、Nehaniv 2005](https://researchprofiles.herts.ac.uk/en/publications/empowerment-a-universal-agent-centric-measure-of-control/)。本项目首版不训练世界模型来计算该量，采用第 7 节明确命名的胜任与控制可靠性近似。

用户提供的 Soul Computing、Curiosity-Critic、专利参数和未指明出处的 MIT 累加器可作为后续研究线索；在未确定对应原文、适用条件和实现收益前，不把它们写成已验证依据或强制依赖。

全局内驱强度可用于诊断，但不作为唯一目标。实际验收查看：持续兴趣、适度休整、问题修复、能力进展和相处质量是否形成可解释的切换，以及每次活动是否留下真实、可续接的经历。

## 20. 源码核对入口

- [现有状态更新](/D:/projects/qq-deepseek/life/state.py)、[人格参数](/D:/projects/qq-deepseek/life/persona.py)、[私聊主流程](/D:/projects/qq-deepseek/bot.py)、[现有 Planner](/D:/projects/qq-deepseek/life/planning.py)。
- [DSH 插件管理工具](/D:/projects/deepseek-harness-master/deepseek-harness-master/packages/boot/plugin-manager/src/tools.ts)、[管理器结果类型](/D:/projects/deepseek-harness-master/deepseek-harness-master/packages/boot/plugin-manager/src/types.ts)、[管理器说明](/D:/projects/deepseek-harness-master/deepseek-harness-master/packages/boot/plugin-manager/README.zh.md)。
- [DSH 工具执行管道](/D:/projects/deepseek-harness-master/deepseek-harness-master/packages/core/tools/src/index.ts)、[工具日志约定](/D:/projects/deepseek-harness-master/deepseek-harness-master/packages/core/agent-loop/src/tool-calls.ts)、[审批服务](/D:/projects/deepseek-harness-master/deepseek-harness-master/packages/interaction/user-approval/src/index.ts)。
- [SDK 当前协议](/D:/projects/deepseek-harness-master/deepseek-harness-master/packages/sdk/protocol/src/types.ts)、[SDK 服务路由](/D:/projects/deepseek-harness-master/deepseek-harness-master/packages/sdk/server/src/server.ts)、[Python SDK](/D:/projects/deepseek-harness-master/deepseek-harness-master/python/sdk/src/deepseek_harness/client.py)、[CLI 插件管理](/D:/projects/deepseek-harness-master/deepseek-harness-master/apps/cli/src/plugin.ts)。

上述内容来自静态源码核对与三个离线纯函数计算。本次没有启动 bot/DSH、安装删除插件、调用模型、变更真实配置或数据库；桥协议与新状态规则均待实施及按第 18 节验收。
