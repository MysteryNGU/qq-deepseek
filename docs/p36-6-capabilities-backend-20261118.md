# P36-6 后端：`/mobile/state` 的能力入口

日期：2026-11-18 11:15–11:22 CST　执行：isolation-verifier

## 部署

```
DECLARED f52dc54bdfa63e516e81fc9f02694602  ACTUAL f52dc54bdfa63e516e81fc9f02694602  MATCH life/companion_mobile.py
```
只改这一个文件，备份 `companion_mobile.py.bak-p366-*`。
真实 `import life.companion` → `IMPORT_OK`。
重启后 `active` / `NRestarts=0` / `/alive=200` / err 0 / `companion-watchdog inactive`。
**部署后 32 秒复查 md5：仍是 `f52dc54b`，没有回滚。**

## 线上实测（真 `/mobile/state`，mob_ token）

```
top-level keys: [active, capabilities, history, history_before, history_limit,
                 life, mobile_device, mobile_requests, name, next_wake, notes, phase, projects, town]
capabilities count: 6
  action.town        ready        小镇现在是连着的。
  action.bilibili    ready        B站后端可用。
  action.dsh         ready
  action.web         unknown      执行器没有把它报成可用；具体原因我这边看不到。
  action.command     unknown      执行器没有把它报成可用；具体原因我这边看不到。
  action.work        unknown      执行器没有把它报成可用；具体原因我这边看不到。
keys per row ok: True     total<=6: True     ready 在前: True
```

## 实现与 capability-engineer 的 patch 的差异（三处，都是刻意的）

1. **不重复算快照。** patch 里 `_collect_capability_brief(core)` 内部又调了
   `core.snapshot()`，但 `state` handler **手上已经有一份**。`snapshot()` 是
   0.8–2.5 秒的活，`/mobile/state` 每次刷新都多付一次没有意义。
   改成 `_collect_capability_brief(core, snapshot_raw)`，隔离测得
   `snapshot_calls_when_passed = 0`。
2. **补 `import logging`。** patch 用了 `log.warning`，但 `companion_mobile.py`
   里**没有 `log` 这个名字**——照抄会在异常分支抛 `NameError`，把「能降级的事」
   变成「整个接口 500」。已加模块级 `log = logging.getLogger('companion.mobile')`。
3. **`_capability_brief` 跳过没有 `capability_ref` 的行**，避免产出
   `{"name": null}` 这种前端没法渲染的条目。

## 三条要求逐条对

| 要求 | 结果 |
|---|---|
| 状态词原样输出，不翻译 | ✅ 线上就是 `ready` / `unknown` 原文；隔离测试里 `needs_setup` 与 `offline` 都被保留 |
| 读不到返回 `None`/`[]`，诚实降级 | ✅ `core=None` → `None`；快照里没这个键 / 类型不对 → `[]`。**不会给一个假的全绿** |
| 不超过 6 条（可用 3 + 不可用 3） | ✅ 隔离测试喂了 8 条（5 ready + 3 非 ready），输出 6 条且 ready 全在前 |

## 隔离验证（部署前）

`.acceptance-local/t16/verify_capabilities.py`，六个判据全绿：

```
C1 shape     keys 严格等于 {name,status,reason}，无 raw/source_ref/evidence
C2 split     8 条输入 → 6 条输出，ready 3 条在前，其余在后
C3 verbatim  ['ready','ready','ready','needs_setup','offline','needs_setup'] 原样
C4 honest    none_core=True, missing_key=[], bad_type=[]
C5 same src  手机侧 status 与 awareness.capabilities() 逐条一致 → matches_model_side=True
C6 no cost   snapshot_calls_when_passed = 0
```

**C5 是这次真正想钉住的一条**：手机看到的 `status` 和模型读到的必须同源。
两处各算一套，正是今晚反复出现的那个根因。

## 一个值得注意的线上现象（不是 bug，但前端要知道）

生产上 `action.web` / `action.command` / `action.work` 报的是 **`unknown`**，
reason 是「执行器没有把它报成可用；具体原因我这边看不到」。

这是因为 `/mobile/state` 拿的快照里 `available_actions` 只有执行器**当轮**报的种类。
`unknown` 是诚实答案（不知道），不是「不能用」。**前端不要把它渲染成红色「不可用」**，
应该显示成「状态未知」。

## 收尾

- 探针设备已清空：中途重复 mint 留了 3 个 `t16-probe-device`，
  已按名字前缀精确清除，`5 → 2`，保留的两台都是「小鱼安卓」真机。
- 过程中我自己犯过一次：token 文件被两条并发的 `> /root/t16token.txt` 写坏，
  导致连续几次 401。**重定向写同一个文件 + 并发 = 内容拼接**，
  这类「配置文件在自己手里被写坏」值得记一笔。
