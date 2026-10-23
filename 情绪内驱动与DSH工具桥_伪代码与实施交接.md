# 情绪内驱动与 DSH 工具桥：伪代码与实施交接

> 2026-10-22 交接更新：以 [自主角色主设计](./长期任务与自然表达_DSH插件借鉴设计.md) 的架构、伪代码与替换清单为当前实施入口。本文 P01–P26 保留作历史算法参考，不再具有冲突时的优先权；不得在新 DSH 主认知之外继续建立旧 Python 规划与调度路径。

日期：2026-10-22。状态：待实施规格。交付对象：负责具体功能实现的 DeepSeek。

主设计：[详细实现设计](/D:/projects/qq-deepseek/情绪内驱动与DSH工具桥_详细实现设计.md)。需求依据：[需求补充](/D:/projects/qq-deepseek/需求补充_私聊陪伴与内驱动.md)。本文件补齐主设计中只有叙述或公式的执行分支；涉及调用顺序、去重、状态迁移和恢复的细节，以本文件的明确化为准，不改变主设计的产品目标。

**本文件的伪代码不是已完成的生产代码。** `repo / cognition / bridge / lifecycle` 等是下文规定的待实现或待适配端口，不能直接声称当前仓库已有这些完整方法。Python 块采用可解析语法表达算法；不承诺单独复制一块便可独立运行。

## 0.0 实施现状（2026-10-22，按仓库实际代码核对）

阶段①②③④均已在工作副本 D:/projects/dsh-bridge-work 上真机跑通，并已接进运行中的 bot（config.toml 的 dsh_* 开关；命令口 `/桥` 可直接看 worker 与工具数）。

已落地（代码位置）：

- qq_life JSON-RPC：packages/sdk/server/src/server.ts:401 `qqLifeHello`，:618 起分发 hello/tools/list/execute/status/cancel；身份核验读 `<profileDir>/.qq-bot-owned.json` 并同时校验 owner 与 home，`profileOwnedByBot` 不再是固定返回值。
- 外部工具 turn：packages/core/agent-loop/src/agent.ts:218 `runExternalToolTurn`，由 SDK 侧 packages/sdk/server/src/server.ts:502 调用；执行所有权、turn/step、取消归一化都在 agent-loop 手里。
- 操作持久化：packages/sdk/server/src/qq-life-operations.ts 写到 `<home>/qq-life/operations.jsonl`，启动 `load()` 把未结束操作标成 unknown；同一 operationId 换参数会抛 `TypeError` 拒绝复用，同参数重放只回历史快照。
- 取消语义：`qqLifeCancel` 先把状态改成 stopping，再 await 真正结束；只有 `settled` 为真才回 cancelled，否则如实说还在停。
- Python 侧：life/services/dsh_bridge.py 读写分离（`_write_lock` 管 stdin，读线程按 JSON-RPC id 分发到 per-id 队列），执行期间 status/cancel 不会被一把锁挡住；life/services/schema.py 建 `bridge_operations` 表，life/services/store.py 提供 `bridge_op_get/put/update/unsettled/recent`。
- Bot 工具面：life/services/tools.py:691 `_dsh_bridge`，action = list / call / status / cancel / plugins / install_plugin / remove_plugin；改环境的动作统一过 `_authorize` 同意门。
- 阶段④自修改闭环：life/services/dsh_workshop.py `DshWorkshop`（快照 → 精确替换 → `tsc --noEmit` → `restart_worker()` + hello/tools 探针 → 失败恢复原文），life/services/schema.py 建 `dsh_edits` 表，store 提供 `add_dsh_edit/update_dsh_edit/get_dsh_edit/list_dsh_edits`；life/services/tools.py `_dsh_bridge` 增加 action = edits / edit / rollback。验证通过才 `caps.bump`，并在生命感运行时开着时结算 `verified_task_satisfaction`。
- 运行中 bot 接线：bot.py 的 Cfg 增加 `dsh_permission_mode`；life/services/core.py 增加 `dsh_workshop`、`settle_verified_task()` 与 `_bridge_report()`，命令口 `/桥`（`cmd == "桥"`）真起一次 worker 报归属核验、工具数与未结算操作。
- 工具超时：life/services/core.py `_tool_timeout()` 给 dsh_bridge edit/rollback/install_plugin/remove_plugin 900s、call 400s，避免长验证被 150s 默认掐断。
- 插件闭环真机结果：`install_bundle` 装 D:/projects/dsh-qq-home/test-plugin → 工具 26→27 出现 qq_life_probe → 调用返回 `pong` → `plugin_remove` 卸载 → 工具回到 26、bundles 清空。

已知约束（环境边界，不是待修缺陷）：

- 本 profile 没有 HMR：安装返回 restart-required，卸载必须由外部进程完成，因此桥改用 `dsh plugin --profile sdk remove <bundle>`（`DshBridge.plugin_remove`，内部先停 worker 再跑 CLI）。
- 执行域按 worker 代数生成（`DshBridge.scope` = `qq-life-<hex>-g<N>`）：会话日志里只要有一次没走完的工具调用，resume 就会报 SessionFormatError，所以不复用旧代执行域，跨重启对账只认操作编号。
- 审批通道：桥默认拒绝服务端审批回调，worker 以 `DSH_PERMISSION_MODE=danger-full-access` 运行，安全由 Bot 的同意门（`_authorize`）把关。
- 本机 node.exe 无法写 D:/projects/qq-deepseek 与 %TEMP%（EPERM），联调要把 home 指到 D:/projects/dsh-qq-home；生产换机器不受此限。

仍待做：

- 审批回调接入 Bot 同意门（`set_approver`），目前一律按拒绝处理。
- 本文档 P16–P23 伪代码的字段名与实现对齐（workerGeneration / requestHash / settled）。

## 0. 交接结论与原稿缺口

原稿已有主流程、状态公式、接口草案和 38 个验收场景，但下列部分缺少完整伪代码：人格迁移、按边界推进欲望、高位周期、心境积分、原因撤回、满足原子结算、候选仲裁、DSH 双向请求泵、插件升级恢复和发布续接。本稿逐项补齐。

另修正一个调度歧义：**新消息作业负责认知与回复；自主复查作业负责生成和选择自主动机；执行作业负责推进已选计划。** 它们共享一套状态，不在每条入站消息末尾立即跑一次自主执行，也不让执行结果递归触发无限模型调用。

## 1. 设计—伪代码覆盖索引

| 主设计项 | 伪代码 | 对应验收 |
|---|---|---|
| 时间、单位、参数与只读状态 | P01、P04、P06 | E01、E02、E09、E20 |
| 人格编译、十四维迁移与版本更新 | P02 | E14、E15 |
| 内稳态、七类需要、动态涨幅 | P03、P04 | E03、E19、E20 |
| 性欲高位、内部缓解、恢复期 | P04、P05、P10 | E21、E22、E24 |
| 语义评价、对象、用户状态与表达 | P07、P08 | E07、E16、X01 |
| 情绪刺激、原因和慢心境 | P06、P08 | E01、E06、E09 |
| 误判撤回、真实修复与关系更新 | P09 | E05、E08、D01 |
| 满足、长期学习、学习进度与胜任 | P10、P11 | E04、E05、E13、E24 |
| 念想自由生成、排序、惯性与安静 | P12、P13 | E10、E11、E15、E17、E18 |
| 唤醒、主流程、打断、恢复 | P14、P15 | E02、E10、E12、B10 |
| 联系冷却与互动边界 | P05、P13 | E23、X01 |
| DSH 启动、握手与工具发现 | P16 | B01、B02 |
| 双向授权与自然确认 | P17 | B03、B08 |
| 工具 turn、执行落盘与结果恢复 | P18、P19 | B02、B03、B07、B08 |
| 插件发现、添加、启停、删除 | P20、P21 | B04、B05 |
| 插件升级、失败回退 | P22 | B06 |
| DSH 修改自身、独立发布、原任务续接 | P23 | B09、B10 |
| 数据结构、迁移、删除与确定性回放 | P24、P25 | E14、D01、D02 |
| 连续生活与体验验收 | P26 | X01、X02；其余场景的组合回放 |

## 2. 实施约定与端口

### 2.1 唯一状态源与不可变上下文

所有真实事件、状态版本、动机、计划、结算和投递归 life 库管理；DSH 只管理其工具执行、插件和会话日志。现有 Brain/evo 的兼容读取不得变成第二套情绪写入者。

```text
ExecutionContext {
  agent_id, scope_id, participant_id, audience_policy_id
  source_refs[], root_experience_id, root_motivation_id
  turn_id?, plan_id?, step_id?
  state_version, input_revision, forget_epoch, policy_epoch
  job_id, job_generation, lease_token
}
```

每次等待模型/工具后先检查上下文。输入 revision 改变可令旧回复失效；一个自主项目是否被取消应查计划与依赖，不把任何新消息都等同于永久取消自主项目。私密输入不可通过改变 scope_id 跨域复用。

区分 `StaleContext` 与 `StateVersionConflict`：前者表示删除、撤权、输入替代或租约丢失，应停止对应动作；后者表示可重试的状态 CAS 冲突，用同一事件/root 重新读取快照并排期，不算新经历或能力失败。一次自身状态提交后，后续回复使用提交后版本的新上下文，不能被自己的更新误判为过期。

两个 root 不混用：`root_motivation_id` 标识持续兴趣/原始缺口，负责预算与项目连续性；`root_experience_id` 标识一段独立入站经历或一次真实工具结果，负责情绪与满足去重。同一项目完成第二个真实步骤可以增加新的经历，换计划 ID 或复述上一步不能创造新经历。

`source_refs` 包含实际喂给模型的完整可见上下文依赖；提案中的直接证据另存为 direct_evidence_refs。删除时同时检查上下文依赖，不能只删掉模型主动引用的几个 ID，却留下可能复述私密内容的旧模型产物。

### 2.2 伪代码端口的具体责任

| 端口 | 必须实现的语义 |
|---|---|
| `repo.tx()` | SQLite 写事务；内部不等待模型、网络或子进程；与现有 Repository.tx 适配 |
| `repo.lock_scope/load/save` | 按作用域读取一个版本化快照；以 version CAS 提交；`lock_scope` 是事务内检查，不额外发明分布式锁 |
| `repo.assert_current(ctx, ...)` | 校验来源仍有效、epoch、作业租约、计划状态；回复另查 input_revision；需要固定模型快照时显式传 expected_state_version；前者失败抛 StaleContext，后者冲突抛 StateVersionConflict |
| `repo.claim_once(key)` | 唯一键插入；成功表示本事务首次应用该效果；事务回滚时标记也回滚 |
| `repo.append_event(...)` | 保存事件、来源、时间和根经历；类型不得伪装为 user_report |
| `cognition.*` | 模型请求，返回结构化提案；录制输入摘要、输出、模型与规则版本，数值不直接写库 |
| `sources.validate(refs, purpose, scope)` | 按用途校验来源；内部动机允许人格/时间窗口，外部事实和任务成功需要对应真实证据 |
| `jobs.*` | 复用 WorkQueue 租约、资源和预算；待确认/离线使用 waiting 或排期，不写成业务失败 |
| `bridge.*` | 实现主设计 qq_life/*；统一 request/operation ID、取消、对账与 schema generation |
| `verifier.run(checks, observations)` | 执行类型化验收；LLM 输出“成功”不算证明；无验证器返回 unknown，不能默认 True |
| `contact.reserve(...)` | 事务内检查关系意愿、未回复、拒绝、冷却与全局额度；不因欲望升高放宽 |
| `lifecycle.external_turn(...)` | DSH 正式执行入口，真实管理 Agent 所有权/状态/turn/取消/whenIdle，不是普通 contextmanager 伪装；已实现为 packages/core/agent-loop/src/agent.ts:218 `runExternalToolTurn`，SDK 侧调用点 packages/sdk/server/src/server.ts:502 |

辅助的数据筛选、哈希、裁剪、排序可作为本地纯函数；关键动作不得留一个永远返回成功的占位实现。未知 schema、未知 job、未知 verifier 必须隔离或报明确 unsupported。

## P01. 配置校验、时钟与单位

增长使用小时，持久时间戳使用 UTC 秒，在函数入口只转换一次。参考涨幅、驱力上限、阈值和恢复期必须显式校验。

```python
def validate_drive_config(c):
    assert c.mode in {"accumulating", "task_derived", "resource_derived"}
    assert 0 <= c.initial_tension < c.ceiling <= 1
    assert 0 <= c.reference_tension < c.ceiling
    assert 0 < c.expression_threshold < c.high_threshold < c.ceiling
    assert c.base_growth_per_hour >= 0
    assert c.high_dwell_hours > 0 and c.recovery_hours >= 0
    assert 0 <= c.regulation_relief_gain <= c.ceiling
    assert 0 <= c.recovery_growth_multiplier <= 1
    assert c.contact_cooldown_hours >= 0
    assert c.max_contact_attempts_per_window >= 1
    return c


def logical_time(anchor, monotonic_now, wall_now):
    # 同一进程以单调经过时间推进；重启锚点不早于保存水位。
    elapsed = max(0.0, monotonic_now - anchor.monotonic_start)
    return max(anchor.saved_logical_at, anchor.logical_start + elapsed, wall_now)


def settlement_day(now, saved_day):
    return max(shanghai_calendar_day(now), saved_day)
```

`logical_time` 本身不写库；提交时间推进时保存水位。启动时建立新单调锚点。离线区间和实际运行区间由进程启动/心跳记录区分，不能只拿高位 wall-time 差值假装一直运行。极端时钟跳变记为诊断；不创造额度、离线任务或虚构释放事件。

## P02. 人格编译与版本迁移

人格文件不变就复用已有编译结果。模型辅助解释不是启动必需步骤，也不默认发送全文。

```python
async def load_persona(path, repo, compiler, outbound_policy):
    source = read_utf8(path)
    digest = sha256(source)
    cached = repo.persona_by_hash(digest, compiler.version)
    if cached:
        return cached
    explicit = compiler.read_explicit_parameters(source)
    unresolved = compiler.required_fields - explicit.keys()
    estimates = {}
    if unresolved and outbound_policy.allows_persona_fragments():
        fragments = compiler.relevant_changed_fragments(source, unresolved)
        raw = await compiler.propose_interpretation(fragments)
        estimates = compiler.validate_quotes_and_bounds(raw, fragments)
    profile = compiler.combine(explicit, estimates, marked_defaults=True)
    with repo.tx():
        previous = repo.current_persona()
        repo.save_persona_once(profile, digest, compiler.version)
        changed = field_diff(previous, profile)
        for state in repo.all_agent_states():
            state.persona_version = profile.version
            state.parameters = rebase_only_changed_parameters(state.parameters, changed)
            # 不改历史、有效承诺、关系证据；不把旧“色欲”原值当新 tension。
            repo.save_migrated_state(state)
        repo.set_current_persona(profile.version)
    return profile
```

`combine`：显式值优先，其次有原文依据的解释，最后带标记缺省；矛盾声明保持 unresolved，不能擅自平均。旧十四维映射为性格调制；intimacy 的新增长配置单独初始化，原有可追溯情绪保留。完整迁移顺序见 P24。

## P03. 需要水平、动态系数与派生压力

```python
def growth_k(c, context):
    if c.mode != "accumulating":
        return 0.0
    persona_factor = context.persona_factor if not c.persona_already_in_base else 1.0
    factor = clamp(persona_factor * context.approved_context_factor,
                   c.growth_multiplier_min, c.growth_multiplier_max)
    if context.in_recovery:
        factor *= c.recovery_growth_multiplier
    return c.base_growth_per_hour * factor / (c.ceiling - c.reference_tension)


def passive_growth(x, c, context, dt_seconds):
    k = growth_k(c, context)
    return c.ceiling - (c.ceiling - x) * exp(-k * dt_seconds / 3600.0)


def need_view(d, c, evidence):
    if c.mode == "accumulating":
        signal = d.tension
    elif c.mode == "task_derived":
        signal = pressure_from_existing_tasks(evidence.valid_open_loops)
    else:
        signal = pressure_from_recorded_load_and_rest(evidence.resource_ledger)
    return {
        "pressure": clamp01(signal + d.activation + d.learned_offset),
        "level": 1.0 - d.tension if c.mode == "accumulating" else None,
        "repeat_penalty": d.satiety,
        "preference": c.persona_preference,
    }
```

`approved_context_factor` 由已验证语义标签查版本化系数表，不直接采纳模型数字。资源压力只由真实负荷和休整计算；competence 增长的是掌握愿望，不修改能力估计；coherence 不创建不存在的承诺。satiety 不再重复扣除已经恢复的需要水平。

## P04. 欲望按时间边界推进、高位与恢复

阈值必须按真实到达时刻切分，不能每次 tick 都重新开始高位计时。`high_elapsed_s` 只累计连续高位中的实际运行时间，离线不补执行调节。

```python
def threshold_crossing_seconds(x, threshold, c, context):
    if x >= threshold:
        return 0.0
    k = growth_k(c, context)
    if k <= 0:
        return float("inf")
    return 3600.0 * log((c.ceiling - x) / (c.ceiling - threshold)) / k


def advance_drive_segment(d, c, span):
    # span 中参数、情境、在线标记不变；不跨满意事件或参数变更。
    d = clone(d)
    pending_regulation = d.phase == "regulation_pending"
    now, end = span.start, span.end
    while now < end:
        recovering = now < d.recovery_until
        context = span.context.for_dimension(d.dimension).with_recovery(recovering)
        crossing_boundary = False
        if recovering:
            stop = min(end, d.recovery_until)
        elif d.tension < c.high_threshold:
            crossing = threshold_crossing_seconds(d.tension, c.high_threshold, c, context)
            stop = min(end, now + crossing)
            crossing_boundary = isfinite(crossing) and now + crossing <= end
        else:
            stop = end
        if stop <= now:
            if crossing_boundary:
                # 浮点表示已无法容纳极短时间差时，只跨已求解的阈值边界。
                d.tension = c.high_threshold
                d.high_since, d.high_elapsed_s = now, 0
                continue
            raise InvalidTimeline("non_advancing_boundary")
        was_high = d.tension >= c.high_threshold
        d.tension = passive_growth(d.tension, c, context, stop - now)
        if recovering:
            d.high_elapsed_s, d.high_since = 0, None
        elif was_high:
            if d.high_since is None:
                d.high_since = now
            if span.online:
                d.high_elapsed_s += stop - now
        elif crossing_boundary:
            d.tension = c.high_threshold
            d.high_since, d.high_elapsed_s = stop, 0
        if d.tension < c.high_threshold:
            d.high_elapsed_s = 0
            d.high_since = None
        now = stop
        d.phase = "recovering" if now < d.recovery_until else (
            "high" if d.tension >= c.high_threshold else "accumulating")
        if pending_regulation and d.phase == "high":
            d.phase = "regulation_pending"
    d.updated_at = end
    d.regulation_due = bool(c.regulation_enabled
        and d.phase in {"high", "regulation_pending"}
        and d.high_elapsed_s >= c.high_dwell_hours * 3600)
    return d


def advance_state_copy(state, until, timeline, config):
    result = clone(state)
    for span in timeline.split(state.updated_at, until,
                               at_context_changes=True, at_parameter_changes=True,
                               at_online_changes=True, at_observations=True):
        for dim, drive in result.drives.items():
            drive.activation *= exp(-LOG2 * span.seconds / config.activation_half_life_s[dim])
            drive.satiety *= exp(-LOG2 * span.seconds / config.satiety_half_life_s[dim])
            if config.drives[dim].mode == "accumulating":
                result.drives[dim] = advance_drive_segment(drive, config.drives[dim], span)
        result.affect, result.mood = advance_affect_and_mood(result, span, config)
        result.resources = integrate_real_load(result.resources, span.load, span.seconds)
    result.updated_at = max(state.updated_at, until)
    return result


def view_at(snapshot, now, timeline, config):
    return advance_state_copy(snapshot, max(now, snapshot.updated_at), timeline, config)


def commit_time_advance(repo, scope, now, timeline, config):
    with repo.tx():
        state = repo.load(scope)
        if now <= state.updated_at:
            return state
        advanced = view_at(state, now, timeline, config)
        repo.save(advanced, expected_version=state.version)
        repo.save_time_cursor(scope, advanced.updated_at)
        repo.enqueue_changed_thresholds_once(state, advanced)
        return advanced
```

边界恰好等于 end 时，要保存 high_since 和 phase。`regulation_due` 只生成一次同周期候选，不能在只读 view_at 中结算缓解。高位尚未满足持续时长时，下一复查时间取“剩余在线高位时长”；离线恢复只安排后续真实运行。

## P05. 自我调节和主动表达

```python
def complete_internal_regulation(repo, ctx, dimension, evidence, now, config):
    with repo.tx():
        repo.assert_current(ctx, require_plan=True)
        state = repo.load(ctx.scope_id)
        d, c = state.drives[dimension], config.drives[dimension]
        if not evidence.is_internal_regulation or not evidence.confirmed:
            return "unknown"
        cycle_before = d.cycle_id
        key = (ctx.scope_id, evidence.id, dimension, "internal_regulation")
        if not repo.claim_once(key):
            return "already_settled"
        if evidence.cycle_id != cycle_before:
            raise InvalidTransition("regulation_cycle_replaced")
        if not d.regulation_due or now < d.recovery_until:
            raise InvalidTransition("regulation_not_due")
        before = d.tension
        d.tension = max(0, before - c.regulation_relief_gain)
        d.high_since, d.high_elapsed_s = None, 0
        d.recovery_until = now + c.recovery_hours * 3600
        d.phase, d.regulation_due = "recovering", False
        d.cycle_id += 1
        repo.append_drive_settlement(ctx, dimension, "internal_regulation", before, d.tension,
                                      cycle_id=cycle_before, evidence_id=evidence.id)
        repo.save(state, expected_version=state.version)
        repo.complete_internal_step_in_same_transaction(ctx.plan_id, ctx.step_id,
                                                        evidence.id, outcome="relieved")
        repo.enqueue_review_after(d.recovery_until)
    return "relieved"


def may_propose_contact(view, dimension, relationship, contact_state, now, config):
    d, c = view.drives[dimension], config.drives[dimension]
    if d.tension < c.expression_threshold:
        return False
    if relationship.explicit_contact_refusal(dimension):
        return False
    if not relationship.allows_expression(dimension):
        return False
    if contact_state.unanswered or contact_state.quiet_now(now):
        return False
    if now < contact_state.last_attempt_at(dimension) + c.contact_cooldown_hours * 3600:
        return False
    return contact_state.within_global_and_dimension_budget(dimension, now)
```

`may_propose_contact=True` 仅允许生成候选，真正发送前仍调用事务内 contact.reserve。明确拒绝不因冷却到期自动撤销；不得自动向其他好友转移亲密请求。内部调节的 confirmed 表示内部调节方案实际完成，不是对方参与或任何虚构身体行为。

## P06. 情绪衰减与可重复的慢心境积分

事件间 `E(t)=B+(E0-B)*exp(-lambda*t)`。心境每个投影轴满足 `M'=k*(U(t)-M)`，若 `U(t)=U0+sum(a_i*exp(-lambda_i*t))`，可直接解析积分。

```python
def mood_axis_after(m0, target_baseline, terms, k, hours):
    decay = exp(-k * hours)
    value = m0 * decay + target_baseline * (1 - decay)
    for amplitude, lam in terms:
        if abs(k - lam) < 1e-12:
            value += amplitude * k * hours * decay
        else:
            value += amplitude * k * (exp(-lam * hours) - decay) / (k - lam)
    return value


def advance_affect_and_mood(state, span, config):
    h = span.seconds / 3600.0
    affect = clone(state.affect)
    mood = clone(state.mood)
    baselines = bounded_baselines(state.persona, span.committed_drive_snapshot)
    lambdas = {k: LOG2 / config.affect_half_life_hours[k] for k in affect.intensity}
    for axis in ("pleasure", "arousal", "coping"):
        # weights 有界，使 target 始终在轴的合法范围；不按每次调用加固定 drift。
        weights = config.mood_projection[axis]
        u0 = weights.constant_from(span.verified_coping, baselines)
        terms = [(weights[k] * (affect.intensity[k] - baselines[k]), lambdas[k])
                 for k in affect.intensity]
        mood[axis] = mood_axis_after(mood[axis], u0, terms,
                                     config.mood_rate_per_hour[axis], h)
    for k in affect.intensity:
        affect.intensity[k] = baselines[k] + (
            affect.intensity[k] - baselines[k]) * exp(-lambdas[k] * h)
    affect.updated_at = mood.updated_at = span.end
    return affect, mood
```

投影权重的绝对值和/偏置必须证明能保持目标范围；若采用非线性限幅，则用主设计固定网格的数值方案，不能套用上面的线性解析式。MoodState 是派生状态，不用每轮的情绪峰值追溯整个过去区间。精力/资源由真实负荷积分，不由一次情绪读取再扣一次。

## P07. 认知评价、用户情绪估计与来源校验

```python
async def propose_appraisal(cognition, event, snapshot, visible_memory):
    packet = bounded_packet(event, snapshot.persona, snapshot.affect,
                            snapshot.relationship, snapshot.open_loops, visible_memory)
    try:
        raw = await cognition.structured_appraisal(packet)
    except TemporaryModelFailure:
        raw = neutral_uncertain_proposal(event.id)
    return raw


def validate_appraisal(raw, event, snapshot, sources):
    require_finite_bounded_numbers(raw)
    if set(raw.source_event_ids) - set(snapshot.visible_event_ids):
        raise InvalidEvidence("invented_or_foreign_source")
    sources.validate(raw.source_event_ids, purpose="affect", scope=snapshot.scope)
    require_supported_subject_and_target(raw, event)
    require_quotes_are_substrings(raw.quotes, snapshot.visible_events)
    if raw.is_quotation_or_hypothesis:
        raw = discount_proposal(raw, reason="not_direct_event")
    # 用户心情是估计，不复制为她对用户的不满。
    user_estimate = validate_user_need(raw.user_state, event, expires=True)
    causes = validate_cause_references(raw.repair_refs, snapshot.active_causes)
    return AppraisalProposal.validated(raw, user_estimate, causes)
```

对 internal_state 的输入只允许支持内在需要和动机，不能当外部事实；对 assistant/self_output 不生成独立证据。未知对象或低置信度时使用普通/不确定评价。旧关键词仅是降级线索，不能决定真实责任或扣信任。

## P08. Appraisal 原子应用与表达

```python
def commit_appraisal(repo, ctx, appraisal, now, config):
    with repo.tx():
        repo.assert_current(ctx, require_input_revision=ctx.is_reply)
        if not repo.claim_once((ctx.scope_id, ctx.root_experience_id, "appraisal")):
            return repo.existing_appraisal(ctx.root_experience_id)
        before = repo.load(ctx.scope_id)
        state = view_at(before, now, repo.timeline(ctx.scope_id), config)
        desire_before = clone(state.drives)  # 同一事件内固定敏感度快照
        for emotion in state.affect.intensity:
            sensitivity = bounded_sensitivity(state.persona, desire_before, emotion)
            weight = structural_evidence_weight(appraisal)
            impulse = clamp(appraisal.delta.get(emotion, 0) * sensitivity * weight,
                            -config.event_cap[emotion], config.event_cap[emotion])
            decayed = state.affect.intensity[emotion]
            state.affect.intensity[emotion] = clamp01(decayed + impulse)
            actual = state.affect.intensity[emotion] - decayed
            repo.add_affect_contribution_once(ctx, appraisal, emotion, actual)
        for e in appraisal.valid_activation_evidence:
            key = (ctx.scope_id, e.root_experience_id, "activation_change", e.dimension)
            if repo.claim_once(key):
                d = state.drives[e.dimension]
                d.activation = clamp_signed(d.activation + e.delta)
                repo.add_drive_settlement(ctx, e, before_after=True)
        apply_distinct_relationship_evidence(repo, state, appraisal.relation_evidence)
        state.user_state = appraisal.user_estimate
        repo.save(state, expected_version=before.version)
        repo.record_state_journal(ctx, appraisal, before, state, now, config.version)
        repo.enqueue_review_if_meaningfully_changed(before, state)
    return state


def expression_plan(persona, user_estimate, affect, relationship, contact_policy):
    mode = user_estimate.need if user_estimate.is_current else "unclear"
    plan = persona.default_expression_for(mode)
    if mode == "quiet_company":
        plan.question_budget = 0
        plan.unsolicited_report = False
    plan.mentionable_causes = visible_relevant_causes(affect, relationship)
    plan = clamp_expression_to_user_preferences(plan, relationship)
    plan.contact_allowed = contact_policy.permits_current_context
    return plan
```

来源与幂等检查在同一事务内。情绪影响表达与候选，但不能改变事实、收件人、工具权限。模型生成回复时只得到有界表达计划与相关原因，不需要朗读所有数值。

## P09. 关系证据、原因修复与误判撤回

```python
def apply_distinct_relationship_evidence(repo, state, evidence):
    for e in evidence:
        sources.validate(e.sources, purpose="relationship", scope=state.scope)
        key = (state.scope, e.root_experience_id, e.kind, e.dimension)
        if not repo.claim_once(key):
            continue
        remaining = repo.remaining_daily_relation_budget(state.scope, e.dimension)
        actual = clamp(e.supported_delta, -remaining, remaining)
        state.relationship[e.dimension] = clamp01(state.relationship[e.dimension] + actual)
        repo.add_relationship_contribution(e, actual)
        repo.spend_relation_budget(state.scope, e.dimension, abs(actual))


def apply_repair(repo, ctx, repair, now):
    with repo.tx():
        repo.assert_current(ctx)
        causes = repo.causes_in_scope(ctx.scope_id, repair.cause_ids)
        require_specific_support(repair, causes)
        if repair.kind == "prior_interpretation_wrong":
            # 追加撤回，不改原话，不重放任何外部动作。
            marker = repo.append_event("appraisal_retracted", ctx,
                                       targets=repair.appraisal_ids, evidence=repair.sources)
            repo.revoke_contributions_of(repair.appraisal_ids, marker.id)
            repo.invalidate_unsent_plans_derived_from(repair.appraisal_ids)
            replay = replay_valid_state(repo, ctx.scope_id, until=now)
            repo.replace_state_after_rebuild(ctx.scope_id, replay)
        else:
            # 真实事件保留；道歉只对有支持的原因形成新的修复贡献。
            for cause in causes:
                cause.status = "resolved" if repair.proves_resolution(cause) else "eased"
                repo.link_repair(cause, repair.sources)
            repo.enqueue_appraisal_once("repair", repair.sources, cause_ids=repair.cause_ids)
```

仅淡化情绪不能自动标为 resolved。撤回需重建情绪、心境、关系、激活与长期偏移；不能只把 cause 标记关闭。初始可信检查点若本身包含已撤回来源，继续向前找干净检查点或从人格初态重建。回放不得执行投递/设备/安装副作用。

## P10. 验证、满足与长期学习原子结算

```python
def verify_outcome(checks, observations, registry):
    if not checks:
        return Verdict("unknown", evidence=[], reason="missing_acceptance_checks")
    if observations.dispatch_uncertain:
        return Verdict("unknown", evidence=[])
    evidence, failed = [], []
    for check in checks:
        verifier = registry.get(check.type)
        if verifier is None:
            return Verdict("unknown", reason="missing_verifier")
        result = verifier(check, observations)
        if result.state == "unknown":
            return Verdict("unknown", evidence=evidence)
        if not result.passed:
            failed.append(result)
        else:
            if not result.evidence_ids or not verified_sources_match_check(result, check):
                return Verdict("unknown", evidence=evidence, reason="missing_independent_evidence")
            evidence.extend(result.evidence_ids)
    return Verdict("partial" if failed and evidence else "failed" if failed else "succeeded",
                   evidence=evidence)


def settle_verified_outcome(repo, ctx, outcome, verdict, now, config):
    with repo.tx():
        repo.assert_current(ctx, require_plan=True)
        event = repo.append_outcome_once(ctx, outcome, verdict)
        if verdict.state not in {"succeeded", "partial"}:
            repo.record_failure_or_unknown_without_reward(ctx, event)
            return event
        state = repo.load(ctx.scope_id)
        effects = derive_only_verified_effects(outcome, verdict)
        for e in effects:
            key = (ctx.scope_id, ctx.root_experience_id, e.kind, e.dimension)
            if not repo.claim_once(key):
                continue
            d = state.drives[e.dimension]
            if e.kind == "activation_change":
                d.activation = clamp_signed(d.activation + e.delta)
            elif e.kind == "interaction_satisfaction":
                require_real_participation_and_matching_relationship(e, outcome)
                d.tension = max(0, d.tension - bounded_satisfaction_gain(e))
                d.satiety = clamp01(d.satiety + repeat_cooldown_gain(e))
                reset_high_counter_and_begin_recovery(d, now, config.drives[e.dimension])
            elif e.kind == "verified_task_satisfaction":
                d.tension = max(0, d.tension - bounded_satisfaction_gain(e))
                d.satiety = clamp01(d.satiety + repeat_cooldown_gain(e))
                if d.tension < config.drives[e.dimension].high_threshold:
                    d.high_since, d.high_elapsed_s, d.regulation_due = None, 0, False
            elif e.kind == "preference_learning":
                repo.queue_independent_learning_evidence(e)
            else:
                raise UnsupportedEffect(e.kind)
            repo.add_drive_settlement(ctx, e, before_after=True)
        repo.save(state, expected_version=state.version)
        repo.remember_observed_experience(event, sources=verdict.evidence)
        repo.record_competence_observation(outcome, verdict)
        repo.update_plan_checkpoint(ctx.plan_id, verdict, outcome.next_checkpoint)
        repo.enqueue_result_appraisal_once(event.id)
    return event


def reset_high_counter_and_begin_recovery(d, now, c):
    d.high_since, d.high_elapsed_s, d.regulation_due = None, 0, False
    d.recovery_until = now + c.recovery_hours * 3600
    d.phase = "recovering" if d.recovery_until > now else "accumulating"
    d.cycle_id += 1


def settle_long_term_preferences(repo, scope, now, config):
    with repo.tx():
        state = repo.load(scope)
        evidence = repo.unsettled_valid_preference_evidence(scope)
        for dimension, group in sorted(group_by_dimension(evidence)):
            roots = independent_roots_across_days(group)
            if roots.local_date_count < 3:
                continue
            proposed = supported_preference_change(roots)  # 不使用单次满足的 delta
            budget = repo.remaining_learning_budget(scope, settlement_day(now, state.last_day))
            old = state.drives[dimension].learned_offset
            new = clamp(old + clamp(proposed, -budget, budget), -0.15, 0.15)
            state.drives[dimension].learned_offset = new
            repo.record_preference_settlement_once(roots, old, new, abs(new - old))
        repo.save(state, expected_version=state.version)
```

partial 只结算已经独立验证的小目标，不领取整个任务完成的满足。`internal_regulation` 单走 P05，不伪装成互动满足。慢学习未达到跨天标准继续保留，额度用尽不换 root 绕过；与原有来源关联以便删除重建。

P08 与 P10 对 activation_change 共用同一根经历/维度去重键；结果投影和后续情绪评价不得分别领取一次。activation 的推荐范围为 [-0.25,0.25]，正负贡献均按实际施加量存储；pressure 最后统一限幅。

## P11. 学习进度与能力估计

```python
def competence_update(history, new_sample, config):
    if new_sample.kind in {"temporary_network_error", "awaiting_permission"}:
        return history  # 不把离线/待确认当能力下降
    history = append_unique_verified_sample(history, new_sample)
    same_family = comparable_samples(history, definition=new_sample.test_definition,
                                     difficulty=new_sample.difficulty)
    if len(same_family) < 2 * config.progress_window:
        return history.with_estimate("unknown", exploration_budget=config.cold_budget)
    older, recent = consecutive_windows(same_family, config.progress_window)
    progress = max(0.0, mean_error(older) - mean_error(recent))
    reliability = independent_success_fraction(recent)
    stable_progress = progress if sufficient_independence(older, recent) else 0.0
    return history.with_metrics(progress=stable_progress, reliability=reliability,
                                sample_count=len(same_family), measured_at=new_sample.time)
```

理解测试与真实应用分别存样本类型；自测答案不能覆盖来源矛盾。随机输出长期无可比较改善时不获得持续进展奖励。经验估计不是严格互信息 empowerment，也不作为修改模型权重的声明。

## P12. 自由目标生成、合并与真实性检查

```python
async def generate_wants(cognition, view, repo, budget):
    # 高位调节是有明确状态依据的候选，不因模型预算耗尽永久饥饿。
    candidates = regulation_candidates_due_once_per_cycle(view)
    if not budget.allow_call(view.root_motivation_id, "want_proposals"):
        return candidates
    raw = await cognition.propose_wants(
        persona=view.persona, affect=view.affect, needs=view.drive_view,
        interests=view.interests, unresolved=view.open_loops,
        evidence=view.visible_memory, allow_quiet=True)
    for item in bounded_list(raw, maximum=6):
        sources.validate(item.sources, purpose="motivation", scope=view.scope)
        if not item.goal or not item.expected_outcome or not item.acceptance_checks:
            continue
        if any(not registered_check(c) for c in item.acceptance_checks):
            item.status = "deferred_missing_verifier"
        signature = derive_signature(item.goal, item.interest_id, item.problem_family)
        old = repo.want_by_signature(view.scope, signature)
        if old and not has_new_independent_evidence(item, old):
            candidates.append(old)  # 不叠加强度、不建立新 root、不绕过完成状态
            continue
        want = bind_motivation_refs(item, view)  # root 由宿主分配，不接收模型自造授权
        candidates.append(repo.upsert_candidate(want, without_reinforcement=True))
    return candidates
```

不强制从 news/group/screen/think 菜单选活动；工具原语与验证类型可以固定，目标内容自由。候选的“将有收益”只存 prediction，不写事实、能力或满足。

根动机必须来自已存在的兴趣 ID、能力缺口 ID、承诺 ID 或确定的内稳态周期 ID；同一个问题换说法仍共享该 root 的预算。语义 signature 可以帮助合并目标，但不得用目标文本 hash 直接替代稳定 root，否则换词就能绕过实验上限。

`regulation_candidates_due_once_per_cycle` 按 `(scope, dimension, cycle_id)` 合并已有候选；只引用真实状态窗口，不生成已完成的经历。候选被正式选中才把 phase 置为 regulation_pending；尚未执行不能降低 tension。调节中断后保留同一周期计划，取消后清除 pending 标记并按当前状态复查。

## P13. 仲裁、惯性、安静与联系预留

```python
def select_want(candidates, view, config):
    if view.user_input_pending:
        return Decision("yield_to_user")
    committed = next_due_accepted_commitment(view)
    if committed:
        return Decision("continue_commitment", committed.id)
    scored = []
    for w in candidates:
        if w.completed and not w.has_new_independent_goal:
            continue
        if w.expired or not w.sources_valid or not feasible_resources(w, view):
            continue
        if w.is_contact and not may_propose_contact(
                view, w.dimension, view.relationship, view.contact, view.now, config):
            continue
        if not effects_currently_allowed(w, view):
            persist_waiting_reason(w)  # 不在选择时执行或默默扩大授权
            continue
        match = verified_cause_and_interest_match(w, view)
        gain = calibrated_predicted_gain(w, view.competence, cold_factor=0.5)
        approach = beta_interest(view) * view.affect.anticipation * match.anticipation
        approach += beta_joy(view) * view.affect.joy * match.joy
        approach += beta_expectation(view) * gain
        repair = sum(beta(k, view) * view.affect[k] * match[k] * feasible_relief(w, k)
                     for k in ("concern", "frustration", "hurt"))
        motivation = clamp01(approach + repair)
        score = (view.persona.autonomy_weight * motivation
                 + continuation_bonus(w, view) + bounded_progress_bonus(w, view)
                 - effort_cost(w) - interruption_cost(w, view)
                 - repeated_failure_cost(w, view) - view.satiety_penalty(w)
                 - unnecessary_contact_cost(w, view))
        scored.append((score, w.id))
    if not scored or max(scored)[0] < config.start_threshold:
        return Decision("quiet")
    best_score, best_id = deterministic_max(scored)
    if view.current_project and current_project_still_feasible(view):
        current_score = score_of_current(scored, view)
        if best_score < current_score + config.switch_margin:
            best_id = view.current_project.id
    return Decision("selected", best_id, components=record_all_components(scored))


def reserve_contact_before_send(repo, ctx, contact_plan, now):
    with repo.tx():
        repo.assert_current(ctx, require_plan=True)
        policy = repo.current_contact_policy(ctx.scope_id)
        if policy.refused(contact_plan.dimension) or policy.has_unanswered():
            return False
        if not policy.allowed_now(now, contact_plan):
            return False
        return repo.reserve_global_and_dimension_contact_once(ctx, contact_plan, now)
```

冷却、未回复、明确拒绝、每日额度取最严格者。unknown 投递保持额度占用，待对账；不能一次欲望循环自动领取一次发送机会。选择理由落库供解释，普通聊天不展示评分表。

首版补齐可执行的调度参数起点：`start_threshold=0.10`、`switch_margin=0.08`、`continuation_bonus` 上限 0.08、`progress_bonus` 上限 0.08、单项成本归一化到 [0,0.25]；beta 六项总和归一化为 1。候选分数相同按当前项目优先，再按持久 want_id 排序。所有参数先作为带版本的缺省，依固定候选回放调整，不能由模型每次临时给权重。

## P14. 作业分派、唤醒、退避与模型调用边界

```python
async def dispatch_job(job):
    handlers = {
        "respond": on_user_message,
        "motivation_review": review_motivation,
        "plan_step": execute_one_step,
        "outcome_project": project_outcome,
        "time_advance": advance_time_job,
        "preference_settle": settle_preferences_job,
        "bridge_reconcile": reconcile_bridge_job,
        "release_observe": observe_release_job,
    }
    handler = handlers.get(job.kind)
    if handler is None:
        return jobs.finish(job, "quarantined", error="unknown_job_kind")
    try:
        await handler(job)
    except StateVersionConflict:
        jobs.requeue_same_event_if_owned(job, reason="refresh_state_snapshot")
    except StaleContext:
        jobs.finish_if_owned(job, "cancelled")
    except TemporaryDependencyFailure as exc:
        jobs.requeue_if_owned(job, exponential_backoff(job.attempts), reason=exc.category)
    finally:
        jobs.ensure_terminal_requeued_or_waiting_if_owned(job)


async def on_user_message(job):
    # 原命令与自然确认保持可用，路由必须使用当轮不可变会话。
    if await existing_command_or_natural_confirmation(job.event, job.context):
        return jobs.finish_if_owned(job, "done")
    snapshot = runtime.snapshot_for(job.context)
    proposal = await propose_appraisal(cognition, job.event, snapshot, snapshot.memory)
    app = validate_appraisal(proposal, job.event, snapshot, sources)
    state = commit_appraisal(repo, job.context, app, clock.now(), config)
    reply_ctx = repo.context_after_own_commit(job.context, state.version)
    plan = expression_plan(state.persona, state.user_state, state.affect,
                           state.relationship, state.contact_policy)
    reply = await cognition.compose_reply(job.event, state.visible_packet(), plan)
    repo.assert_current(reply_ctx, require_input_revision=True)
    await delivery.send_confirmed_segments(reply, reply_ctx)
    jobs.finish_if_owned(job, "done")


async def review_motivation(job):
    view = runtime.view_for_job(job)
    if view.user_input_pending:
        return jobs.requeue(job, delay=config.idle_review_after_input)
    if not meaningful_change_or_review_due(view):
        return jobs.requeue_at(job, next_meaningful_review_time(view))
    candidates = await generate_wants(cognition, view, repo, jobs.budget(job))
    with repo.tx():
        repo.assert_current(job.context, expected_state_version=view.version)
        decision = select_want(candidates, runtime.current_view(job.scope), config)
        repo.save_choice(decision)
        if decision.kind == "selected":
            intent = repo.activate_bounded_plan(decision.want_id)
            jobs.enqueue_once("plan_step", intent.id, root=intent.root_motivation_id)
        else:
            repo.schedule_next_review_without_self_reinforcement(decision)
        jobs.finish_if_owned(job, "done")
```

输入接收时暂停自主新步骤，已开始动作安全收束。一次结果只 enqueue 一个 outcome_project；投影之后 enqueue 复查，禁止在同一调用栈递归调用 life_cycle。模型评价、候选生成分别有预算和 next_review_at；静止状态不每秒请求模型。

`existing_command_or_natural_confirmation` 必须适配现有命令和自然批准入口，重新校验当前 owner 与原申请的会话/动作；不是从旧 peer→最新轮次映射取上下文。明确承诺只能在可执行任务已持久化后表达；没有执行路径时回应能力边界，不凭生成一句承诺创建已接受任务。

其他分派项均为有明确映射的薄适配：`time_advance→P04`、`preference_settle→P10`、`bridge_reconcile→P19`、`release_observe→P23`。`outcome_project` 只加载已经落盘的操作结果，以该操作自己的不可变上下文调用 P10，已结算则返回原结果。

下一唤醒时间取已知最早的任务期限、恢复期结束、在线高位剩余时间、资源重试、兴趣复查或空闲复查；无新线索的空闲复查从 10min 退避至最多 60min。待确认由确认事件唤醒，不每 30s 当作失败重试。等待与网络问题不计为新的能力失败根经历。

## P15. 计划步骤、打断与恢复

```python
async def execute_one_step(job):
    plan = repo.get_plan(job.plan_id)
    if not plan.active or not plan.dependencies_valid:
        return jobs.finish(job, "cancelled")
    if runtime.user_input_pending() and plan.origin == "initiative":
        return jobs.finish(job, "paused", checkpoint=plan.checkpoint)
    step = plan.next_unverified_step()
    if step is None:
        return complete_plan_only_if_all_checks_proven(plan, job)
    if not jobs.acquire(job, step.shared_resource):
        return jobs.requeue(job, delay=config.resource_retry_s)
    existing = repo.operation_for_step(plan.id, step.id)
    if existing and existing.status in {"dispatching", "stopping", "unknown"}:
        jobs.enqueue_once("bridge_reconcile", existing.id, root=plan.root_motivation_id)
        return jobs.finish(job, "unknown", checkpoint=plan.checkpoint)
    ctx = repo.bind_step_context(job, plan, step)
    if step.kind == "internal_regulation":
        if not repo.internal_step_result_exists(plan.id, step.id):
            evidence = verifier_registry.verify_selected_regulation_cycle(plan, ctx)
            # evidence 由宿主校验当前周期、high_elapsed 和选中计划后生成，非模型自报。
            complete_internal_regulation(repo, ctx, step.dimension, evidence, clock.now(), config)
        # 状态缓解和步骤完成同事务；崩溃恢复读取结果，不第二次验证旧周期。
        return jobs.finish_if_owned(job, "done")
    outcome = await bridge.execute(step, ctx)
    verdict = verify_outcome(step.checks, outcome.observations, verifier_registry)
    settle_verified_outcome(repo, ctx, outcome, verdict, clock.now(), config)
    if verdict.state == "succeeded":
        jobs.finish_if_owned(job, "done")
        jobs.enqueue_next_step_after_commit(plan.id)
    else:
        jobs.finish_if_owned(job, "unknown" if verdict.state == "unknown" else "waiting",
                             checkpoint=repo.current_plan_checkpoint(plan.id))


async def interrupt_and_resume(plan, new_input):
    repo.request_pause(plan.id, reason=new_input.id)
    running = repo.current_operation(plan.id)
    if running and running.cancellable_read:
        await bridge.cancel(running.id)
    # 写操作不先释放资源；等待确认收束，由 P19 负责清理。
    if repo.operation_still_active(plan.id):
        return "pausing"
    repo.pause_at_checkpoint(plan.id)
    return "paused"


def resume_if_eligible(plan):
    if plan.cancelled or plan.expired or not sources.still_valid(plan.source_refs):
        return False
    if not dependencies_and_grants_current(plan):
        return False
    jobs.enqueue_once("plan_step", plan.id, root=plan.root_motivation_id)
    return True
```

幂等 plan job key需包含下一步或作业代数，不用一个永久 done 的 job ID 阻挡后续续接；根预算 ID 不随新代数改变。

## P16. DSH 启动与工具发现

```python
async def ensure_dsh_bridge(paths, config):
    cli = discover_supported_cli_or_prepared_runtime(paths)
    if cli is None:
        return DependencyState("unavailable", diagnostic="需要构建或配置受支持的 DSH CLI")
    home = paths.project / "data" / "dsh-home"
    ensure_sdk_profile_manifest(home, inspected_template_version=config.dsh_version)
    await install_pinned_bridge_bundle_if_missing(cli, home, config.bridge_artifact)
    client = await start_owned_harness_client(cli, home, profile="sdk",
                                               public_launcher_options=True)
    start_request_pump_before_first_rpc(client)
    await sdk_initialize(client, provider=config.provider, model=config.model)
    hello = await rpc(client, "qq_life/hello", {"protocol": 1})
    if hello.protocol != 1 or not hello.profile_is_owned_by_bot:
        await close_owned_process_tree(client)
        return DependencyState("incompatible_bridge")
    return DshBridge(client, worker_generation=hello.worker_generation)


async def discover_tools(bridge, ctx):
    entries, cursor = [], None
    while True:
        page = await bridge.request("qq_life/tools/list", {
            "executionScope": ctx.execution_scope, "cursor": cursor})
        validate_schema_page(page)
        entries.extend(page.entries)
        cursor = page.next_cursor
        if cursor is None:
            break
    return freeze_registry(entries, tool_generation=page.tool_generation,
                           worker_generation=bridge.worker_generation)
```

初始化与安装用 supported CLI/profile 管理，不启动桌面 EXE 当 CLI；不改桌面 profile，不依赖私有 SDK argv。schema 只序列化名称、参数、输出合约与归属，不把函数/凭据传给模型。schema generation 改变使未来调用重新绑定；管理器本次合法改动导致 generation 改变，不应把该操作的已观察结果当陈旧响应丢弃。

## P17. 双向授权与审批请求泵

```python
def sdk_request_pump(client, host, stop_signal):
    while not stop_signal.is_set():
        try:
            request = client.next_request()
        except TransportClosedError:
            return
        try:
            if request.method == "qq_life/authorize":
                with host.repo.tx():
                    op = host.repo.operation(request.payload["operationId"])
                    allowed = bool(op and op.status in {"prepared", "dispatching"}
                        and op.request_hash == request.payload["requestHash"]
                        and host.repo.context_is_current(op.context)
                        and host.policy.allows_exact(op.grant, op.plan, op.tool, op.arguments,
                                                     op.resources, op.context))
                client.respond(request.id, {"decision": "allow" if allowed else "deny"})
            elif request.method == "qq_life/approval":
                # 不能阻塞整个泵；独立的有界处理任务等待已有自然确认流程。
                host.approvals.enqueue_callback(request, client)
            else:
                client.respond_error(request.id, -32601, "unsupported bridge callback")
        except InvalidProtocol as exc:
            client.respond_error(request.id, -32602, str(exc))


async def handle_approval_callback(item, host):
    bound = host.policy.validate_pending_call(item.payload)
    if not bound:
        return item.reply({"outcome": "unavailable"})
    existing = host.policy.match_existing_exact_grant(bound)
    if existing:
        return item.reply({"outcome": "allowed-once"})
    request_id = host.approvals.persist_once(bound)
    await host.approvals.ask_in_original_authorized_conversation_once(request_id)
    decision = await host.approvals.wait_bounded(request_id)
    if decision.approved and host.policy.call_still_exact_and_current(bound):
        host.policy.record_exact_grant(bound)
        return item.reply({"outcome": "allowed-once"})
    return item.reply({"outcome": "denied" if decision.denied else "cancelled"})
```

审批只对当前原始调用与权限范围有效。审批等待有独立截止时间，等待期间续租，停止时收束 DSH open turn；关闭 SDK 时请求泵由传输关闭唤醒，不遗留永久阻塞线程。不能在执行线程等待 execute 时让同一线程再等待 callback，造成死锁。

## P18. DSH 路由、正式工具 turn 与调用幂等

以下使用 Python 语法描述 DSH TypeScript 侧算法；实施时必须采用该仓库的 Context、Service、品牌 ID 和事件类型，不能把这些拟接口冒称现有导出。

```python
async def dsh_rpc_route(method, params, service):
    if method == "qq_life/hello":
        return service.hello_and_version()
    if method == "qq_life/tools/list":
        ctx = service.resolve_owned_execution_scope(params)
        return service.paginated_schemas(ctx.agent)
    if method == "qq_life/execute":
        return await service.execute(params)
    if method == "qq_life/status":
        return service.lookup_owned_operation(params)
    if method == "qq_life/cancel":
        return await service.cancel_and_wait_for_settlement(params)
    raise MethodNotFound(method)


async def dsh_execute(service, request):
    spec = validate_wire_and_bind_scope(request)
    with service.operations.tx():
        old = service.operations.get(spec.operation_id)
        if old:
            if old.request_hash != spec.request_hash:
                raise InvalidProtocol("same_id_changed_arguments")
            if old.status != "prepared":
                return old.public_status_or_result()  # 已开始/已收束均不重复派发
        else:
            service.operations.insert_prepared(spec)
    # 远端等待不持数据库事务。
    authorization = await service.peer.request("qq_life/authorize", spec.authorize_payload())
    if authorization.decision != "allow":
        return service.operations.finish_before_dispatch(spec, "denied")
    try:
        service.assert_current_worker_and_tool_generation(spec)
        agent = await service.owned_agent(spec.execution_scope)
        definition = service.tools.get(spec.tool, agent)
        validate_against_actual_tool_schema(spec.arguments, definition)
    except BeforeDispatchValidationError as exc:
        return service.operations.finish_before_dispatch(spec, "failed_before_dispatch", exc)
    if not service.operations.cas_prepared_to_dispatching(spec.operation_id):
        return service.operations.get(spec.operation_id).public_status_or_result()
    outcome = await run_external_tool_turn(service.lifecycle, agent, spec)
    with service.operations.tx():
        service.operations.record_result(spec.operation_id, outcome,
                                         tool_generation_after=service.current_tool_generation())
    return outcome


async def run_external_tool_turn(lifecycle, agent, spec):
    # lifecycle 是必须在 agent-loop 实现的正式入口，不是只写几条日志。
    async with lifecycle.acquire_external_execution(agent, spec.signal):
        turn = lifecycle.start_turn(agent)
        step = lifecycle.start_step(agent, turn)
        call = lifecycle.append_tool_call(agent, turn, step, spec)
        try:
            result = await lifecycle.tools.execute({
                "agent": agent, "callId": call.id, "name": spec.tool,
                "arguments": spec.arguments, "signal": spec.signal})
            lifecycle.append_tool_result(agent, turn, step, call, result)
            return normalize_tool_result_without_success_guessing(result)
        except BaseException as exc:
            result = classify_started_operation_error(exc)
            lifecycle.append_tool_result(agent, turn, step, call, result)
            return result
        finally:
            lifecycle.close_step_and_turn_after_drain(agent, turn, step)
            # 所有权释放/idle 在 async context 的真正收束点发生。


async def bridge_pre_execute(exec, service, next_policy):
    # 注册在 DSH 真实 tools/pre-execute 管道；映射由服务/执行作用域持有。
    op = service.operation_for_bound_call(exec.agent, exec.callId)
    if op is None:
        return await next_policy()  # 保持普通非桥调用的原有策略
    if exec.signal.aborted:
        return {"kind": "cancel"}
    current = await service.peer.request("qq_life/authorize", op.authorize_payload())
    if current.decision != "allow":
        return {"kind": "deny", "reason": "bound operation is no longer authorized"}
    return await next_policy()
```

新增业务在 bridge 插件中；SDK server 只转发 namespace。`mode=native` 使用真实 tools 管道，保留前置策略、审批、后置规则、结果投影、HMR 释放和完整调用日志。握手时或排队前的一次授权不能代替调用体前的再检查；注册适配使用 DSH 的实际 next() waterfall 约定。若进程崩溃来不及闭合，由启动修复将调用标 unknown；不靠 finally 保证进程被杀后仍能写结果。

## P19. Python 执行、超时、取消与对账

```python
async def bridge_execute(bridge, step, ctx):
    with repo.tx():
        repo.assert_current(ctx, require_plan=True)
        op = repo.prepare_bridge_operation_once(ctx, step,
            request_hash=canonical_hash(ctx.scope_id, ctx.plan_id, ctx.step_id,
                                        step.tool, step.arguments, step.resources))
        if op.has_final_result:
            return op.result
        if op.status in {"dispatching", "stopping", "unknown"}:
            return Outcome("unknown", operation_id=op.id)
        repo.mark_dispatch_intent(op.id)
    try:
        result = await rpc_in_thread(bridge.client, "qq_life/execute", op.wire_request)
    except (TimeoutError, TransportClosedError):
        repo.mark_unknown_if_no_final_result(op.id)
        jobs.enqueue_once("bridge_reconcile", op.id, root=ctx.root_motivation_id)
        return Outcome("unknown", operation_id=op.id)
    return validate_and_store_wire_result(op, result)  # late/forgotten 正文不提交


async def reconcile_operation(bridge, op):
    status = await bridge.request("qq_life/status", {"operationId": op.id})
    if status.has_final_result:
        return validate_and_store_wire_result(op, status.result)
    if status.is_running:
        return requeue_reconcile_with_backoff(op)
    # not_found 也不能证明外部动作没有发生。
    observed = await reconcile_known_effect(op.tool, op.arguments, op.expected_checks)
    if observed.proven:
        return store_reconciled_result(op, observed)
    repo.keep_unknown(op.id)
    return Outcome("unknown", next_safe_step="缺少外部结果证明，停止重复执行")


async def cancel_operation(bridge, op):
    repo.mark_stopping(op.id)
    result = await bridge.request("qq_life/cancel", {"operationId": op.id})
    if result.settled:
        repo.store_cancel_outcome(op.id, result)
        jobs.release_resource_after_settlement(op.resource)
    else:
        jobs.keep_resource_owned_while_stopping(op.resource)
        jobs.enqueue_once("bridge_reconcile", op.id, root=op.root_motivation_id)
```

只读操作允许按单独规则重新读取；写入、发送、安装、删除和发布不根据超时自动重试。Python 与 DSH 各有调用记录，以 operationId 对账，不假装有跨进程原子事务。丢失最终响应时绝不整轮重规划。

状态值固定为 `prepared / dispatching / stopping / result_recorded / denied / failed_before_dispatch / cancelled / unknown`。只有 `denied` 或 `failed_before_dispatch` 且 DSH 已确认调用体未开始时，刷新授权/依赖后才可为同一步创建新的 attempt ID；原 root 预算不变。`unknown`、`dispatching`、`stopping` 不允许靠新 ID 再执行。DSH 本侧 prepared 的同 ID 重入仍须经过授权与 CAS，最多一个调用体开始。

## P20. DSH 插件发现、安装、启停与业务验证

```python
async def acquire_capability(gap, bridge, ctx):
    bundles = await bridge.tool("plugin_manager", {"action": "list_bundles"}, ctx)
    target = exact_compatible_target_from_inventory(bundles, gap)
    if target is None:
        indexes = await bridge.tool("plugin_market", {"action": "list_indexes"}, ctx)
        entries = await search_existing_indexes_only(indexes, gap.normalized_need, bridge, ctx)
        target = select_exact_compatible_target(entries, gap.checks)
    if target is None:
        return CapabilityResult("waiting", reason="no_verified_install_target")
    change = await bridge.tool("plugin_manager", {
        "action": "install_bundle", "target": target.spec,
        **target.explicit_registry_argument()}, ctx)
    result = decode_change_result(change)  # 工具 value 为 JSON 字符串时按声明解码
    return await apply_plugin_change_result(result, gap, bridge, ctx)


async def apply_plugin_change_result(result, gap, bridge, ctx):
    if result.application == "restart-required":
        repo.save_plugin_restart_checkpoint(gap, result)
        await bridge.restart_owned_runtime_after_operations_drain()
    elif result.application == "overridden":
        return CapabilityResult("waiting", reason="higher_layer_override")
    elif result.application in {"failed", "cancelled"}:
        repo.record_actual_plugin_failure(result)
        return CapabilityResult(result.application, reason=result.error)
    elif result.application != "applied":
        raise InvalidProtocol("unknown_application_state")
    tools = await discover_tools(bridge, ctx)
    binding = bind_discovered_tools_to_gap(tools, gap)
    if not binding:
        return CapabilityResult("installed_not_callable")
    observation = await run_independent_business_probe(binding, gap.probe, bridge, ctx)
    if not observation.verified:
        return CapabilityResult("callable_not_verified")
    repo.activate_capability_version(binding, observation.evidence)
    return CapabilityResult("active", evidence=observation.evidence)


async def set_plugin_enabled(bridge, entry, enabled, ctx):
    require_entry_from_current_inventory(entry)
    action = "set_bundle" if entry.is_bundle else "set_plugin"
    result = await bridge.tool("plugin_manager", {
        "action": action, "target": entry.exact_id, "enabled": enabled}, ctx)
    return await verify_saved_and_runtime_plugin_state(result, entry, enabled, bridge, ctx)
```

pendingBuilds、不兼容版本和管理组件保护按管理器原始结果处理。没有得到特定许可时不追加 approvedBuilds/acceptRisk。普通包安装、挂载、工具可见和业务验证为四个不同状态，不能用字符串“装完了”判成功。

## P21. 删除 DSH 插件

```python
async def remove_plugin(bridge, exact_bundle, ctx):
    inventory = await bridge.tool("plugin_manager", {"action": "list_bundles"}, ctx)
    bundle = require_exact_removable_bundle(inventory, exact_bundle)
    await pause_dependent_plans_and_drain_calls(bundle)
    result = decode_change_result(await bridge.tool("plugin_manager", {
        "action": "remove_bundle", "target": bundle.name}, ctx))
    if result.error_code == "stop-profile":
        repo.save_offline_plugin_maintenance(ctx, bundle)
        await bridge.stop_owned_runtime_and_wait_tree_exit()
        result = await supported_dsh_cli_plugin_remove(bundle.name, same_home=True)
        await bridge.start_and_handshake()
    elif result.application == "restart-required":
        await bridge.restart_owned_runtime_after_operations_drain()
    elif result.application != "applied":
        return RemovalResult("not_removed", diagnostics=result)
    inventory_after = await bridge.tool("plugin_manager", {"action": "list_bundles"}, ctx)
    tools_after = await discover_tools(bridge, ctx)
    if still_installed_or_owned_tools_active(bundle, inventory_after, tools_after):
        return RemovalResult("unknown", diagnostics="磁盘或运行态仍有残留")
    repo.mark_capability_unavailable(bundle.name)
    return RemovalResult("removed")
```

卸载 profile 所有权与正在执行的 bundle 分开处理；管理桥、审批、日志和插件管理必需组件不可自主移除。不直接删除 node_modules 目录，不只删除 bot 清单就报告卸载成功。

## P22. 插件升级与失败恢复

```python
async def upgrade_plugin(bridge, bundle, new_target, ctx):
    async with maintenance_lease(bundle.profile):
        old = await capture_exact_bundle_version_sources_and_probe(bundle, bridge, ctx)
        require_immutable_restore_artifact_or_fetchable_pinned_version(old)
        require_supported_data_compatibility(old, new_target)
        await pause_dependent_plans_and_drain_calls(bundle)
        journal = repo.begin_plugin_maintenance(old, new_target, phase="prepared")
        removed = await remove_plugin(bridge, bundle.name, ctx)
        if removed.state != "removed":
            return repo.finish_maintenance(journal, "remove_failed", removed)
        repo.set_maintenance_phase(journal, "old_removed")
        installed = await install_exact_target_and_probe(new_target, old.probe, bridge, ctx)
        if installed.state == "active":
            repo.finish_maintenance(journal, "verified_new", installed)
            return resume_valid_dependent_plans(bundle)
        repo.set_maintenance_phase(journal, "rolling_back")
        await remove_partial_new_bundle_if_present(new_target, bridge, ctx)
        restored = await install_exact_target_and_probe(old.target, old.probe, bridge, ctx)
        if restored.state == "active":
            return repo.finish_maintenance(journal, "restored_old", restored)
        repo.mark_capability_unavailable(bundle.name)
        return repo.finish_maintenance(journal, "recovery_failed", restored)
```

启动恢复先查 maintenance journal 与实际 inventory：根据 observed 版本继续验证或恢复，不盲目重做 remove/install。恢复旧代码不回滚用户消息、删除标记或插件业务数据；需要数据迁移但没有可验证降级协议时不自动升级。

## P23. 用 DSH 修改自己、发布观察与原任务续接

```python
async def improve_self(proposal, ctx, bridge, controller):
    artifact = controller.prepare_candidate(proposal, source_hash=repo.current_source_hash())
    candidate_ctx = bind_candidate_workspace(ctx, artifact.path)
    # 模型给出 patch 意图，DSH read/edit/write/pwsh 只改候选并提供观察。
    observations = await execute_candidate_edit_plan(proposal, candidate_ctx, bridge)
    comparison = controller.evaluate_with_external_oracles(artifact, proposal.acceptance_checks)
    if comparison.regression or comparison.critical_failed:
        return controller.reject_preserving_production(artifact, comparison)
    if not comparison.proven_improvement:
        return controller.keep_inconclusive(artifact, comparison)
    deployment = controller.request_publish_after_base_hash_check(artifact)
    repo.link_proposal_to_release(proposal.id, deployment.id)
    jobs.enqueue_once("release_observe", deployment.id, root=ctx.root_motivation_id)
    return "waiting_for_independent_publisher"


async def observe_release_job(job):
    deployment = controller.deployment(job.deployment_id)
    if deployment.phase in {"queued", "prepared", "installed", "observing"}:
        return jobs.requeue(job, delay=config.release_poll_s)
    if deployment.phase == "observed":
        with repo.tx():
            repo.record_verified_improvement_once(deployment)
            proposal = repo.proposal_for_release(deployment.id)
            repo.mark_proposal_published(proposal.id)
            for plan in repo.original_dependent_plans(proposal):
                if plan.active and sources.still_valid(plan.sources):
                    jobs.enqueue_resume_after_dependency_verified(plan)
    elif deployment.phase in {"rolled_back", "conflict", "recovery_failed"}:
        repo.keep_proposal_unpublished_with_diagnostics(deployment)
    jobs.finish_if_owned(job, "done")
```

独立发布器执行现有本机 stop→换代码→重启→观察/恢复机制。无真实任务时 evidence_pending，不用“进程活着”证明改进。自修不编辑 persona、固定测试、权限或回退控制器；发布成功的记录最终进入 P10，仅一次结算已证实的能力改善。

## P24. 最小持久记录与迁移

建议的表结构骨架如下；实施时先核对现有 schema，做版本化迁移，不直接覆盖旧表：

```sql
CREATE TABLE affect_contributions (
  id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, root_id TEXT NOT NULL,
  appraisal_id TEXT NOT NULL, effect_kind TEXT NOT NULL, dimension TEXT NOT NULL,
  delta REAL NOT NULL, source_ids TEXT NOT NULL, rule_version TEXT NOT NULL,
  created_at REAL NOT NULL, revoked_at REAL, supersedes_id TEXT
);
CREATE UNIQUE INDEX affect_active_effect ON affect_contributions
  (scope_id, root_id, effect_kind, dimension) WHERE revoked_at IS NULL;

CREATE TABLE drive_settlements (
  id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, root_id TEXT NOT NULL,
  dimension TEXT NOT NULL, effect_kind TEXT NOT NULL, cycle_id TEXT NOT NULL DEFAULT '',
  before_value REAL NOT NULL, after_value REAL NOT NULL,
  source_ids TEXT NOT NULL, payload TEXT NOT NULL, rule_version TEXT NOT NULL,
  created_at REAL NOT NULL, revoked_at REAL
);
CREATE UNIQUE INDEX drive_active_effect ON drive_settlements
  (scope_id, root_id, dimension, effect_kind, cycle_id) WHERE revoked_at IS NULL;

CREATE TABLE bridge_operations (
  id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, plan_id TEXT NOT NULL, step_id TEXT NOT NULL,
  request_hash TEXT NOT NULL, context TEXT NOT NULL, tool_name TEXT NOT NULL,
  arguments TEXT NOT NULL, status TEXT NOT NULL,
  worker_generation TEXT NOT NULL, tool_generation INTEGER NOT NULL,
  result TEXT, artifact_refs TEXT NOT NULL DEFAULT '[]', updated_at REAL NOT NULL
);
```

去重唯一键不包含新规则版本，防止换 version 再领取一次奖励；规则升级必须先撤销/替代旧贡献并重建。根 ID 已含 agent 归属且再绑定 scope，不能跨主体碰撞。

```python
def migrate_state_schema(repo, target_version, profile):
    with repo.tx():
        if repo.schema_version >= target_version:
            return "already_current"
        repo.apply_additive_schema_changes(target_version)
        for old in repo.legacy_state_snapshots():
            new = new_state_from_profile(profile, old.scope)
            new.affect = migrate_valid_caused_affect(old)
            new.relationship = preserve_valid_relationship_evidence(old)
            new.open_loops = preserve_existing_commitments(old)
            new.drives = initialize_seven_drives(profile, explicit_defaults=True)
            repo.retain_legacy_snapshot_readonly(old)
            repo.save_migrated_state(new)
        repo.set_schema_version(target_version)
```

配置字段迁移先完成再切主写入者；旧欲望没有独立来源时不强行当成长久偏好。DSH 本侧 operations 表使用该运行时支持的持久后端，实现同等唯一键与阶段语义，不要求 Python 直接跨进程写 DSH 私有数据库。

## P25. 删除、撤回与确定性回放

```python
def forget_sources(repo, deletion_ledger, scope, requested_roots):
    intent = deletion_ledger.commit_content_free_intent(scope, requested_roots)
    with repo.tx():
        epoch = repo.bump_forget_epoch(scope)
        affected = descendants_only(repo, requested_roots)
        # 共同摘要失效，不反向删除它的其他独立父来源。
        repo.redact_events_memories_and_indexes(affected, epoch)
        repo.revoke_contributions_and_settlements(affected)
        repo.invalidate_jobs_and_unsent_payloads(affected, scope, epoch)
        repo.clear_affected_bridge_body_caches(affected)
        state = replay_valid_state(repo, scope, until=clock.now())
        repo.replace_state_after_rebuild(scope, state)
        repo.mark_deletion_intent_applied(intent.id, epoch)


def replay_valid_state(repo, scope, until):
    checkpoint = repo.latest_clean_checkpoint(scope)
    state = checkpoint.state if checkpoint else initial_state_for_first_recorded_profile(scope)
    records = repo.recorded_transitions_after(scope, checkpoint)
    for record in records:
        if record.retracted or not repo.sources_valid(record.sources):
            continue
        rules = replay_rule_registry.require(record.rule_version)
        state = rules.advance_time(state, record.at, record.recorded_timeline)
        state = rules.apply_recorded_semantics(state, record.model_output,
                                               record.verified_observations,
                                               dispatch_external=False)
    return replay_rule_registry.advance_to(state, until, known_passive_time_only=True)
```

不支持的旧规则版本应报不可完整重放，不能默默用最新版算法解释旧记录后声称完全一致。重放要恢复当时的人格/参数版本和录制模型输出，不重新调用模型，也不真的发消息、装插件、改代码。删除账本在恢复旧库后、任何检索之前应用。

DSH 仍运行的写调用可能完成实际副作用；删除不能声称撤销已发生动作。晚到结果只保留必要内容无关的状态/回执，旧正文、关系、满足、缓存与后续计划不得复活；独立 DSH 持久正文也必须登记并清理。

## P26. 验收与交付方法

```python
def run_acceptance_suite(implementation, fixtures):
    for case in fixtures.design_cases_E01_to_E24:
        run_with_fixed_clock_and_recorded_model(implementation, case)
    for case in fixtures.bridge_cases_B01_to_B10:
        run_real_dsh_profile_composition(implementation, case,
                                       external_network="fixture", use_real_plugins=True)
    for case in fixtures.deletion_replay_cases_D01_D02:
        run_cross_connection_and_crash_recovery(implementation, case)
    for case in fixtures.relationship_cases_X01_X02:
        replay_multi_day_conversations_then_review(implementation, case)
    assert no_production_database_was_used()
    assert no_real_contact_received_fixture_messages()
```

每个测试必须断言具体状态、实际结果或持久记录；不得把“生成了文档/存在函数/返回 ok”作为业务通过。DSH 关键测试要通过真实 profile＋Loader 组合，模拟外部网络/模型即可，不用假的 tools 对象代替全部链路。场景参数和预期值独立于候选实现保存。

建议先实现以下可运行的代表性测试，然后扩展至完整 38 项：

```python
def representative_state_checks(engine, fixtures):
    c = fixtures.intimacy_config
    start = fixtures.drive_at(reference_tension=0.5)
    once = engine.advance(start, seconds=3600)
    many = start
    for _ in range(60):
        many = engine.advance(many, seconds=60)
    assert close(once, many, abs_tolerance=1e-6)
    assert engine.pressure_after_negative_correction(start) <= engine.pressure(start)
    satisfied = engine.apply_real_satisfaction(start, fixtures.valid_interaction)
    settled = engine.daily_settle(satisfied)
    assert settled.learned_offset == start.learned_offset
    high = fixtures.continuous_online_high_state(hours=c.high_dwell_hours)
    relieved = engine.complete_regulation_once(high, fixtures.regulation_evidence)
    duplicate = engine.complete_regulation_once(relieved, fixtures.regulation_evidence)
    assert duplicate.state == relieved.state
    assert not engine.contact_allowed(fixtures.explicit_refusal_with_high_desire)
```

## 3. DeepSeek 实施顺序与完成定义

| 里程碑 | 实施内容 | 交付门槛 |
|---|---|---|
| M1 | P01–P06、P24 状态动力学与迁移 | E01–E04、E09、E19–E22通过；七类需要与亲密独立配置 |
| M2 | P07–P11、P25 认知/关系/满足/修复 | 语义对象、撤回重建、满足去重与删除回放通过 |
| M3 | P12–P15 自主主流程 | 无新指令可生成一个有效目标，实际执行并验证，回写满足与下一步；用户可打断恢复 |
| M4 | P16–P19 真实 DSH 桥 | 可从 bot 调用真实 read 工具，具有完整 turn、双向授权、超时与重启对账 |
| M5 | P20–P22 插件完整生命周期 | 真实 bundle 添加/启停/删除/升级失败恢复，安装与可用分别验证 |
| M6 | P23 自修改发布与原任务续接 | DSH 改候选，外部验收，独立切换、观察/回退，进度与删除标记不回滚 |
| M7 | P26 全矩阵与体验记录 | 38 项设计验收及相关既有回归通过；人工体验与离线机制分别报告 |

M3 在 M4 未完成时只能使用已经存在且真实可调用的工具适配，不能伪造 DSH 调用成功；M4 完成后切换到统一桥，不长期维护两条能重复执行同一动作的路径。

每个里程碑提交：改动文件、schema/配置迁移、真实测试命令与结果、未覆盖场景、剩余缺口。阶段通过不等于初版 T01–T62 全部通过；最终继续按初版与补充文档对照。

## 4. 可直接交给 DeepSeek 的任务说明

```text
请在 D:\projects\qq-deepseek 实施以下两份设计：
1. 情绪内驱动与DSH工具桥_详细实现设计.md
2. 情绪内驱动与DSH工具桥_伪代码与实施交接.md

需求定位是私聊情感陪伴机器人，兼秘书和助手，少量好友；保留一键启动与原有聊天方式。
主线是情绪驱动的“动机→选择→执行→验证→记忆与满足→后续选择”。
各种欲望各有常态涨幅与动态调制，性欲/亲密是独立维度，包含高位、调节与恢复周期。
请先读取当前代码与适用 AGENTS.md，按 M1–M7 依赖顺序实施，不覆盖用户既有修改。
DSH 软件在 D:\projects\dsh；源码在 D:\projects\deepseek-harness-master\deepseek-harness-master。
使用 DSH 插件管理体系和实际 tools 管道；qq_life/* 与外部工具 turn 已按第 0.0 节落地，后续改动以 server.ts / dsh_bridge.py 的实际字段为准，不再按“待新增”当作空缺。
程序在本机正常进程执行，不要求 Docker；候选目录、版本、验证和恢复仍需真实实现。
请对每个覆盖项落实真实代码、来源/状态持久化、接线与测试，不停留在服务接口或返回成功的桩。
运行中的副作用、未知结果、删除屏障与用户打断按文档恢复；不要连接真实联系人做测试。
本次文档中的参数是工程起点，不作为人类生理定律；不得通过修改参数/测试来伪造进步。
每阶段给出实际检查证据；未完成和未验证如实列出，不把伪代码或接口存在当作功能交付。
```

## 5. 本次文档复核范围

P01–P26 覆盖主设计全部可执行机制；背景理论、源码事实、行为示例和实施顺序无需各自重复一段独立算法，分别由覆盖索引定位到实际执行代码。

交接文件的完整性只表示“设计规则已有对应算法和验收入口”，不表示实现已验证、真实模型表现已验证，或所有 DSH 版本均兼容。实施仍须读取目标源码的实际类型与构建规则，并用真实组合测试证明接通。
