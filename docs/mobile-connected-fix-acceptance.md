# 手机 connected=false 修复的验收清单（P21）

日期：2026-11-17 20:45 CST。执行：prerequisites-engineer（t28）。
本文**替换**旧的 P21 验收清单，不可追加；旧清单已标记 SUPERSEDED（见 §6）。
证据口径：每条写实际命令与实际输出；没做的写"未验证"，不用推断补。

---

## 1. 结论先行

| 项 | 状态 | 说明 |
| --- | --- | --- |
| A 在线判定放宽到"90 秒内注册过或" | **已修复并有测试** | 锁屏断流后 90 秒内 `connected` 仍为 true；真派动作仍要求活连接 |
| B 断流不再删设备记录 | **已修复并有测试** | 记录保留，1 小时（测试记录）/ 24 小时（真实设备）未注册才淘汰 |
| C 配对码可重新生成 | **已实现并有测试** | 新码作废旧码，但不影响已配对手机；owner 两种入口 |
| D 清理验收残留 | **已完成** | 删掉 2 条 `verification`，剩 2 条 `小鱼安卓` |
| 真实手机端到端 | **未验证** | 没有手机 token；需要用户装 APK、保持在线后复测 |

**不要据此说"手机能用了"**：服务端判定已修，但"手机 App 在实际锁屏/后台时是否仍每 90 秒注册一次"只能由真机确认（§5）。

---

## 2. A：在线判定（`life/companion_phone.py`）

改动：
- `register()` 记 `last_seen=time.time()`（内存，不持久化）。
- 新增 `online_devices(now)`：`now - last_seen <= RECENT_SECONDS(90)`。缺失、非数值、布尔值或未来时间戳（超过 60 秒时钟偏差）一律不算在线。
- `context()` 用它替换原来的"必须有活 SSE"判定；`enabled` 与 `operations` 非空两个条件保留；每行新增 `has_stream` 供排查。
- `run()` 派发前额外要求该设备此刻有活连接，没有就返回 `phone_unavailable`（"手机当前没有活动连接"），**不会把动作发给没人接收的队列**。

证据（生产同构探针，在服务解释器下加载线上模块）：

```
{"recent_connected": true,
 "recent_row": {"device_id": "locked-phone", "enabled": true, "operations": ["info","screenshot"],
                "has_stream": false},
 "stale_connected": false, "window": 90}
```

隔离测试（`tests/test_phone_presence.py`，沙箱内跑，15/15 通过）：
- `test_registered_phone_counts_online_without_any_stream`
- `test_registration_window_is_ninety_seconds`（89.5 秒在线 / 90.5 秒不在线）
- `test_recent_registration_survives_stream_loss_exactly_as_required`（注册→SSE→断流仍 true；再超窗变 false）
- `test_enabled_and_operations_still_gate_the_row`
- `test_missing_or_bogus_last_seen_is_never_online`
- `test_dispatch_still_needs_a_live_stream`（**放宽不导致误派**）
- `test_dispatch_reaches_the_live_stream`

---

## 3. B：断流不删记录（`life/companion_phone.py`）

改动：
- `disconnected()` 只 `fail_device`（把该设备未完成的操作标 `phone_offline`）+ `prune()`，不再 `devices.pop()`。
- 新增 `prune(now)`：测试名（`verification`）1 小时未注册淘汰，其余 24 小时淘汰；`register()` 与 `disconnected()` 都会调用。
- `register()` 保留原记录里的 `name`（从 `mobile-devices.json` 取一次），并在名字命中测试名时标 `test=True`。

证据：`{"forgotten": ["locked-phone"], "forget": 3600}`（测试记录 1 小时后被淘汰）。

测试：`test_disconnect_keeps_the_device_record`、`test_record_is_forgotten_only_after_the_retention_window`、`test_test_devices_are_forgotten_sooner`、`test_registration_keeps_the_record_name`。

---

## 4. C：配对码可重新生成（`life/companion_mobile.py`）

改动：
- `create_pairing_code(home, state_dir=None)`：生成新 6 位码，24 小时有效，写 `issued='owner_regenerate'`、`replaces_used`。**只作废未使用的旧码**；`mobile-devices.json` 不动，已配对手机继续用原 token（"丢 token 的手机能回来，其他手机不掉线"）。
- `pairing_status(home)`：只报能不能配、过期时间、上次用掉时间，**从不返回仍然有效的码**。
- owner 入口一（HTTP）：`GET /mobile/pairing`（查状态）、`GET /mobile/pairing?action=regenerate`（换新码），鉴权用**服务端 token**，手机 token 拿不到（否则一台手机能替另一台取码）。
- owner 入口二（运维命令行）：`tools/pair-mobile-device.py --home <home> [--list] [--regenerate] [--forget <name> --confirm <name>]`。

证据：`test_pairing_state_reports_used_code_as_unusable`、`test_new_code_is_issued_and_old_one_dies`、`test_regenerated_code_can_pair_a_new_phone`、`test_pairing_route_is_owner_only`（手机 token → 401）。

**未验证**：新码在真实手机 App 里走完整配对流程（需要用户操作手机，且需要主人拿到新码）。

---

## 5. D：清理设备残留（生产 `mobile-devices.json`）

清理前清单（`tools/pair-mobile-device.py --list`）：

```
15dcbfe0eb9c… id=c652e42f… name='小鱼安卓'  created=69.1h_ago
2cfc7189a3af… id=36223861… name='小鱼安卓'  created=68.6h_ago
e570702a018f… id=5f35b541… name='verification' created=20.3h_ago
c5f22c1a1f49… id=8744318e… name='verification' created=20.3h_ago
pairing code: unusable (used_at=1794845555.9)
```

依据：
- `verification` 两条来自验收脚本的固定设备名（`docs/mobile-sse-diagnosis.md` §2.2 记载），不是手机会用的名字；保留无益且会污染判定。
- `小鱼安卓` 两条是主人真实客户端的名字，未动。

执行：`--forget verification --confirm verification` → `forgot 2 device(s)`。清理后只剩两条 `小鱼安卓`。
唯一副作用：`mobile-devices.json` 改成 `companion:companion 600`（原本 `0600 companion`），见 §7 事故。

---

## 6. 服务重启与事故记录

- 重启前核对：`actions_running=0`、`actions_pending=1`（等待中，不是执行中）、单元 ExecStart 未变、磁盘 93%（见风险）。
- 第一次重启：服务循环崩溃 `NRestarts=6`，`启动失败（PermissionError）`，根因是我在 D 步骤做 `chown root:companion`，而服务以 `companion` 身份读该文件 → 拒绝。
- 修复：`chown companion:companion` + `chmod 600`，`systemctl reset-failed`，重启。
- 第二次重启结果：

```
ActiveState=active  SubState=running  MainPID=1063305  NRestarts=0
ExecMainStartTimestamp=Tue 2026-11-17 20:34:51 CST
LISTEN 127.0.0.1:8765
/health → 200 {"ok": true, "desktop_connected": false}
```
- 崩溃窗口：约 20:33:46–20:34:51（约 65 秒），期间服务不可用；恢复后 4 分钟内日志无 Traceback/ERROR。

---

## 7. 仍未验证 / 需要用户确认

1. **真机端到端**：需要用户装最新 APK、保持手机在线后看 `phone_context.connected`。没有手机 token，`tools/stage-c/sse_diag.py <token>` 本轮**未跑**。
2. **手机后台注册频率**：方案前提是"手机在后台仍按 20 秒左右注册"。若某版本 App 在后台停止注册，90 秒后仍会显示离线——这是诚实结果，不是新缺陷。
3. **配对流程真机验证**：没做。
4. **磁盘**：`df -h /` 显示 49G 用 44G（**93%**，剩 3.6G）。重启不增占用，但任何写库/写会话都可能因 ENOSPC 失败，建议用户尽快清理。
5. **`/tmp` 被其他成员覆盖**：发现 `/tmp/precheck.py` 在本任务开始前已被别的成员写入（内容为 `budget_mode` 自检）。并行成员请各自用独立目录（本任务用 `/tmp/qq-p21-work`）。

---

## 8. 复跑命令

```bash
# 逻辑测试（隔离沙箱，十几秒）
python -B -X utf8 tests/run_isolated_module.py tests.test_phone_presence

# 云端只读：设备清单 + 配对状态
/opt/qq-deepseek/.venv-companion/bin/python tools/pair-mobile-device.py --home /var/lib/companion/xiaoyu --list

# 拿到手机 token 后（真机验证）
python3 -B -X utf8 tools/stage-c/sse_diag.py <token>
```

---

## 9. 旧清单

上一版 P21 验收清单（记在 `docs/mobile-sse-diagnosis.md` 的诊断与建议部分）**SUPERSEDED**：它以"未修复"为前提，本文是修复后的验收口径。
