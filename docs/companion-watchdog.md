# 自愈 watchdog：改坏了自己能回滚并告诉她

日期：2026-11-17。执行：prerequisites-engineer（t33）。状态：**已实现、已在生产演练验证、**单元当前 `disabled`，等用户明确授权才启用。
环境：云端 `47.104.8.84`，`companion.service`（User=companion），源码 `/opt/qq-deepseek`。

---

## 1. 它是什么

一个**独立的 systemd 单元** `companion-watchdog.service`。它不在小鱼进程里——因为它要救的正是那个进程。

每次 `companion.service` 重启，它进入 60 秒观察窗口：

| 观察结果 | 它做什么 |
| --- | --- |
| 单元 `active` + `GET /alive` 200 + 窗口内 journal 无 `Traceback`/`CRITICAL`/`启动失败` | 判 `healthy`：把当前源码快照标为"已验证"，删掉更旧的快照。**不动服务、不写库** |
| 单元不是 `active/activating` | 判 `failed` |
| `/alive` 非 200 且窗口已过 | 判 `failed` |
| 窗口内出现 Traceback 等错误 | 判 `failed` |
| 连续失败超过 3 次 | 停止自动回滚，写一条"自动恢复停下了"的通知，等人处理 |
| 没有已验证快照 | 不猜，什么都不回滚，只记日志 |

`failed` 时它做的事：还原已验证快照 → `systemctl restart companion.service` → 往 `life.db` 写一条事件，让她下次醒来知道"刚才那次修改让服务起不来，已经回滚"。

---

## 2. 组件

| 文件 | 作用 |
| --- | --- |
| `ops/companion-watchdog.py` | 全部逻辑：快照、健康判定、回滚、通知、防呆 |
| `ops/companion-watchdog.service` | 单元定义（`Restart=always`，root 运行，需调 `systemctl`） |
| `life/companion_remote.py` 的 `GET /alive` | 免鉴权存活探针，只回 `{"alive": true, "pid": N}` |
| `life/companion_phone_view.py`（早先） | 与本次无关，队列里顺带修的 |
| 状态目录 | `/var/lib/companion/xiaoyu/data/watchdog/`（`state.json`、`snapshots/`、`objects/`） |

**快照范围**：`/opt/qq-deepseek` 下 760 个源文件（`*.py`/`*.md`/`*.sh`…），**排除** `data/`、`toolchain/`、`vendor/`、`.venv-companion/`、`node_modules/`、`config.toml`、日志与缓存。实测 7.0 MB（内容寻址 + 硬链接去重）。
**它从不回滚数据**：`life.db`、`config.toml`、人格文件都不在快照里。

---

## 3. 怎么启用 / 停用 / 紧急停掉

```bash
# 启用（立刻开始值守，开机自启）
systemctl enable --now companion-watchdog.service

# 只看状态
systemctl is-active companion-watchdog.service
journalctl -u companion-watchdog.service -n 20 --no-pager

# 紧急停掉（立刻停手，但开机还会自启）
systemctl stop companion-watchdog.service

# 彻底停用（现在停 + 以后不自启）
systemctl disable --now companion-watchdog.service
```

**启用后它在什么情况下会动服务**：只有上面表格里的两种 `failed`——单元不是 active，或窗口内 `/alive` 不通/有 Traceback。**正常 healthy 时它只写一行日志，不碰服务。**
**它不会做**：不会改 `life.db`、不会改 `config.toml`、不会动 `toolchain/`、`vendor/`、不会发 QQ、不会替你判断"这次改动好不好"——它只回答"改完之后还能不能起来"。

---

## 4. 踩坑记录（比演练更值钱的部分）

### 坑一：`/health` 需要鉴权 → watchdog 裸请求永远 401 → **把好版本判成坏版本并真的回滚了**

`CompanionServer.auth` 给除 `POST /mobile/pair` 之外的所有路径加了 Bearer 校验，`/health` 也在其中。watchdog 不带 token → 永远 401 → 永远判 `failed`。

**22:28:39 真实发生了**：服务本身完全正常，watchdog 判 `failed (/health not 200)`，执行了 `systemctl restart companion.service`，还给她写了一条"你刚才的修改导致起不来，已回滚"——**但她这次什么都没改**。这条错误通知已按批准删除（`evt_e2bae7ff62346e96`，后 22:43 又误报一次 `evt_1f23003360947d0e`，也删了）。

修法：新增免鉴权 `GET /alive`（只回 `{"alive": true, "pid": N}`，无状态、无 token、无主人数据），watchdog 改打它，并且**不持有 server-token**。

### 坑二：豁免写在 `MobileAPI.authenticate` 里没有用

第一版把 `/alive` 的豁免写进了 `MobileAPI.authenticate`——但 `CompanionServer.auth` 的判断是「路径以 `/mobile/` 开头 → 交给手机鉴权；否则走服务端 token」。`/alive` 压根到不了手机鉴权那一层。实测仍是 401。
修法：豁免必须写在 **middleware `CompanionServer.auth` 本身**：

```python
async def auth(self, request, handler):
    if request.path=='/alive':
        return await handler(request)      # 存活探针，无凭证
    if request.path.startswith('/mobile/'):
        ...
```

### 坑三：把"有没有说话"当健康标准 → 又误报一次

第二版判定要求"60 秒内至少有一次认知活动"。重启后她本来就可能安静几分钟，于是 22:43:28 判 `failed (no cognition turn in 60s)`，又回滚、又重启、又写了一条错误通知（`evt_1f23003360947d0e`，已删）。

修法：**健康只由"进程起来了没 + 有没有报错"决定**。认知活动降级为日志里的描述（`entry_served, turns=N`），不参与判定。理由：代码坏了的第一表现是 import 失败或进程起不来，这两个 `systemctl` 和 journal 都看得见。

### 坑四（演练方法本身）：改一个"没人 import 的新文件"证明不了什么

第一次演练我新建了一个语法错误的 `life/watchdog_drill_probe.py`，服务照常健康——因为那个文件根本不参与启动。watchdog 判 `healthy`，快照被"验证通过"。
**真正的演练必须改坏一个会被 import 的模块**，让服务在启动时真的失败。

### 坑五：PowerShell 的 `Get-Content`/`Set-Content` 往返把 UTF-8 中文写坏

一次用 PowerShell 做字符串替换，中文被按 GBK 解成私用区字符，文件直接语法错误。凡是要改含中文的 Python 源文件，用 Python 自己做读写（`io.open(..., encoding='utf-8')`），或者一次性写完整文件。

---

## 5. 生产演练（2026-11-17 22:49–22:50，真实故障路径）

**演练设计**：把 `life/companion_phone_view.py`（服务启动时会 import 的模块）末尾追加一行非法语法 → 重启 → 让 watchdog 自己发现、自己回滚。

| 时刻 | 事件 | 证据 |
| --- | --- | --- |
| 22:49:17 | 记录原始 sha256、watchdog `inactive`、`known_good=115735ff87524436` | `S1 original_sha256: 8d75774a…` |
| 22:49:19 | 启动 watchdog，`systemctl restart companion.service`（MainPID 1154628 → 1156327） | `S4 restart_at` |
| 22:49:24 | 新进程启动时 Traceback（import 失败） | journal |
| 22:49:33 | watchdog 判 **failed**：`4 journal error(s): … Traceback`，`failure #1 of 3` | `verdict=failed` |
| 22:49:45 | **自动回滚**：`rolled back to 115735ff87524436: restored=2 removed=1`，随即 restart（MainPID 1156521） | `rolled back …` |
| 22:49:55 | 判 **healthy**，新版本被标为已验证 | `snapshot … verified good` |
| 22:50:00 | 文件 sha256 与原始**完全一致**；`/alive` 200；`/health`(token) 200；watchdog 停下并保持 `disabled` | `matches_original: True` |

**通知事件原文**（`life.db`，`SELECT` 出来的）：

```json
{"id": "evt_4d72c8b55b3cb3b7", "agent": "xiaoyu", "conversation": "qq:private:763282103",
 "kind": "message", "source_kind": "self_observation",
 "external_id": "watchdog:rollback:Tue 2026-11-17 22:49:19 CST",
 "payload": {"watchdog": "rollback",
   "reason": "4 journal error(s): … Traceback (most recent call last):",
   "rolled_back_to": "115735ff87524436",
   "restored": ["life/companion_phone_view.py", "ops/companion-watchdog.py"],
   "removed": ["life/watchdog_drill_probe.py"],
   "text": "刚才那次源码修改让服务起不来；已经回滚到上一次能用的版本并重新拉起。这次改动被丢弃了，需要的话可以重新来一遍。"}}
```

**这次演练证明的**：
1. 真的坏了 → 真的检测到（依据是 journal 里的 Traceback，不是靠猜）；
2. 真的恢复了（文件字节级还原，sha256 一致）；
3. 真的重新拉起了（新 MainPID，`/alive`、`/health` 都 200）；
4. 真的告诉她了（事件落在主人私聊会话，`self_observation`，认知轮能读到）；
5. 恢复正常后没有"再来一次"的循环（判 healthy 后 `failures` 归零）。

**这次演练没证明的**：
1. **没证明"进程根本起不来"这种最坏情况**——真实演练里进程是起来了再抛 Traceback。如果进程连 `active` 都到不了（比如 `companion.py` 本身语法错），watchdog 走的是"单元非 active → failed"这条分支，只有离线测试覆盖，没在生产实测。
2. **没证明连续失败 3 次后的熔断**（只在离线测试里验证）。
3. **没证明"服务能起但业务不可用"**（例如能回话但记忆写不进去）——当前判定不看业务，这类故障它不会处理。
4. **没验证 QQ 通道在回滚后是否恢复**（NapCat 当前 disabled，没法测）。
5. 演练中 `removed` 里那条 `life/watchdog_drill_probe.py` 是上一步演练遗留的空文件，被一并清理掉了，不影响结论。

---

## 6. 已知限制

- 只能回滚**源码**，不回滚数据。数据库迁移类改动若不可逆，它救不回来。
- 快照粒度是"整棵源码树"，单次约 7 MB、760 文件；改动很大时快照耗时增加（实测 < 2 s）。
- 它用 `systemctl` 操作单元，所以必须以 root 运行；它不做任何鉴权判断，靠 systemd 单元文件本身约束。
- 判定窗口 60 秒。如果某个改动会让服务在 60 秒之后才崩，它不会介入。
- **默认关闭**：单元是 `disabled`，因为启用之后它会在判定失败时接管重启决策，这需要用户明确同意。

---

## 7. 复跑

```bash
# 离线测试（隔离沙箱，16 项，不碰生产）
python -B -X utf8 tests/run_isolated_module.py tests.test_companion_watchdog

# 只判定不动手
/opt/qq-deepseek/.venv-companion/bin/python /opt/qq-deepseek/ops/companion-watchdog.py --once --dry-run

# 手工打一次快照
/opt/qq-deepseek/.venv-companion/bin/python /opt/qq-deepseek/ops/companion-watchdog.py --snapshot

# 手工还原（不依赖 watchdog 单元，出事时用这个）
python - <<'PY'
import importlib.util, json
state_dir='/var/lib/companion/xiaoyu/data/watchdog'
spec=importlib.util.spec_from_file_location('wd','/opt/qq-deepseek/ops/companion-watchdog.py')
wd=importlib.util.module_from_spec(spec); spec.loader.exec_module(wd)
state=json.load(open(state_dir+'/state.json'))
print(wd.restore('/opt/qq-deepseek', state_dir, state['known_good']))
PY
systemctl restart companion.service
```
