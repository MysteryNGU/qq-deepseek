# 部署事故记录与本轮上线（t3）

日期：2026-11-17 23:52～2026-11-18 00:05 (+08:00)
执行：capability-engineer（t3）。两条事故都是本轮真实发生的，记录在这里供以后所有部署参考。
**本文件不下结论说"以后不会再犯"，只记事实、根因和必须遵守的规矩。**

---

## 0. 本轮上线事实

| 项 | 值 |
|---|---|
| 上线时刻 | 2026-11-17 **23:52:08 CST**（`ActiveEnterTimestamp`） |
| MainPID / NRestarts | `1195160` / `0` |
| `systemctl is-active` | `active` |
| `/alive` | `200` |
| 同步文件 | `life/{companion_awareness,companion_context,companion,companion_agents,companion_presence,companion_experience,companion_memory}.py` |
| 校验方式 | 本地与云端逐个 `md5sum` 比对，七项全同 |
| 备份 | `/opt/qq-deepseek/life.bak-t3-20261117-235020/`（改动前五个文件） |

改动相机位（上游依据见 `docs/capability-chain-audit.md` §7）：

- 靶点 A：`_settle` 认 `completed`（`SETTLED_OK`，保留 `succeeded` 兼容）
- 靶点 B：`_channel_facts()` + `channel.*.usable`，通道状态来自登录核验，不再由 `available_actions` 推断
- 靶点 C：`TRACKED_KINDS` + `_offline_reason()`，掉线的 kind 留 `needs_setup`/`offline` 条目并带具体原因
- 靶点 E：`OP_FIELDS` 按 op 过滤 `_action_fields`，`inspect` 带 `goal` 不再 `TypeError`
- 追加：`guard.watchdog` 条目（**只读 `systemctl` 实测**）、`presence.phone` 拆 `paired`/`online`、`GUIDES['memory']` 补 `op=used`
- 未做：靶点 F（队长指示暂缓）、靶点 D 的 `phone_context` `dispatchable` 细化

---

## 1. 事故 A：ImportError 让服务崩了约 3 分钟

### 现象

23:51:10 起 `companion.service` 反复启动失败：

```
启动失败（ImportError）
  File "/opt/qq-deepseek/life/companion.py", line 44, in <module>
    from . import companion_experience as experience
```

systemd 重试 6 次（restart counter 到 2 后被人工介入），23:52:08 恢复。**约 3 分钟内服务不可用。**

### 根因

并行开发的两个成员改同一个功能域：t4 新增 `life/companion_experience.py` 并让 `companion.py` import 它；t3 把 `companion.py` 同步上云，**但 `companion_experience.py` 没有跟着上去**。目标机上存在"引用了不存在的模块"的代码。

这不是某一个人的疏忽，是**并行开发 + 分头部署**的结构性风险：谁先部署都可能在中间态崩。

### 必须遵守

1. **部署前验证所有被 import 的模块都已在目标机存在**。最小做法：同步后、`systemctl restart` 前，在目标机跑一次
   `python -c "import life.companion"`（或 `python -m py_compile` 全量被改文件 + 一次真实 import）。
   `py_compile` **不够**——它只查语法，查不出缺失的被 import 模块。
2. **跨成员的依赖文件要么一起上，要么等上游先上**。谁引入新模块，谁负责确认对端部署清单里有它。
3. 重启前先看 `systemctl is-active` 的**当前**状态并准备好回滚（本轮有 `life.bak-t3-*`，但没用上，因为补文件比回滚快）。

---

## 2. 事故 B：测试脚本把生产磁盘写满，她的认知轮丢了 3 分钟写入

### 现象

23:59:24 生产日志：

```
File "/opt/qq-deepseek/life/companion_life_view.py", line 36, in _rows
    return core.repo._all(sql, tuple(args))
sqlite3.OperationalError: database or disk is full
```

`df -h /` 当时 49G 已用到满（探针跑之前 35G/75%）。删除临时目录后恢复：**27G 可用（45%）**，`pragma quick_check` = `ok`，00:00:10 之后 `disk is full` 计数为 0。

### 根因

我写只读探针时图省事，用 `shutil.copytree` 把整个 `/var/lib/companion/xiaoyu/data/companion` 复制到 `/tmp` 想去隔离环境里组装一次请求体。那个目录里有大量 B 站帧图和 mp4，直接撑爆根分区。

**这不是"只读脚本"，它有写副作用——写在 /tmp，伤在根分区，最终伤到生产库的写入。**

### 必须遵守

1. **任何测试/探针脚本不得 `copytree`／`rsync`／`cp -r` 生产数据目录。** 也包括 `data/`、`artifacts/`、`.venv*`、B 站缓存。
2. 要验装配，**只读特定文件**（`qq.json`、`config.toml`、`persona.md` 这类几十字节~几 KB 的），或者直接用 `__new__`/`SimpleNamespace` 壳对象构造最小 core，根本不碰真实数据。
3. **探针脚本要先算占用**：动手前 `du -sh` 源目录 + `df -h /` 看余量，写进脚本开头。
4. 探针退出时清理自己建的临时路径（本轮漏了，靠手工 `rm -rf /tmp/t3-probe-*` 补的）。

### 影响面

写库失败的那几轮，她拿不到本轮认知结果——**等于她的认知轮丢了约 3 分钟的数据**。这种伤害不会在服务状态里显示成"故障"（`active` 一直是 active），只有翻 journal 才看得到。以后验证任何"服务健康"都要顺手 grep 一次 `disk is full` 和 `OperationalError`。

---

## 3. 判据冲突：watchdog 的实测与文档相反

规格 `docs/capability-acceptance-spec.md` F11 写 `watchdog = enabled + active`；本轮实测（23:47 与 00:00 两次）：

```
systemctl is-active  companion-watchdog.service → inactive
systemctl is-enabled companion-watchdog.service → disabled
journal: Nov 17 23:16:25 ... Deactivated successfully.
```

队长已确认：**判据以 `systemctl` 实测为准，不读文档**。所以现在她说"没人替我兜底"是实话，说"有 watchdog 会回滚"才是错的。

`guard.watchdog` 条目因此按实测输出：`active → ready`、`inactive → offline`、跑不了 `systemctl → unknown`（明说"不知道，不能断言有也不能断言没有"）。

---

## 4. 测试状态（如实标注）

本地 `life.tests.test_companion_awareness`、`life.tests.test_companion_experience`、
`tests.test_capability_cognition`、`tests.test_phone_presence` 共 **94 项**：

- 单独跑四个套件：各套件均 `OK`
- 合并运行：**有一次出现 1 个 failure**，之后**连续 9 次未复现**
- 判断：**怀疑 Windows 临时目录竞争（本仓库已知噪声），未钉死**。不声称 100% 稳定。

`test_projection_stays_small`（4000 字符上限）**阈值未改动**即通过。做法是：
`ready`/`not_ready` 从"重列全部名单"改为"数量"（名单本就在 `usable`/`unusable` 里），
并给不可用项排优先级，让"手机为什么用不了"不被无名条目挤出摘要。
生产实投影 **1987 字符**。

---

## 6. 附注：00:05:18 的第二次重启（非 t3 触发）

t3 在 00:05 前后只写文档、未再同步或重启，但 `ActiveEnterTimestamp` 变成 **2026-11-18 00:05:18**，
MainPID `1195160 → 1209257`。journal 记录是**干净的计划内重启**（`Stopping ... Deactivated successfully ... Started`），
不是崩溃重启（`NRestarts=0`）。同一时刻磁盘占用从 21G 降到 8.2G，判断是另一位成员在清理现场并重启。

同期 `life/companion.py` 在云端被改动（md5 `0cbed5ca…` → `7e15be30…`），新增了模型输出容错提取
（`_json_balanced_object` / `_LAST_PARSE_METHOD` 等）。**t3 的改动仍在**（`SETTLED_OK`、`offline_reasons`、
`tool_instructions_with_offline` 三处实测都在），已把云端版本拉回本地，避免下次同步覆盖他人工作。

重启后实测：`active`、`/alive=200`、`IMPORT_OK`、`NRestarts=0`。t3 此后不再动生产。

---

## 6. 事故 C：拼接点两侧形状不一致，每轮认知都崩（已修）

### 现象

生产日志每轮拼 instructions 时抛：

```
AttributeError: 'tuple' object has no attribute 'get'
```

### 根因

`_messages()` 把 `offline_reasons()` 的返回值硬转成 `(kind, reason)` **元组**，
而 `tool_instructions_with_offline()` 按 **dict** 读 `item.get('kind')`。
两侧形状各自成立，拼在一起就崩。**纯函数单测全绿，真实 `_messages` 全崩——
因为没有一条测试测过这个拼接点。**

### 修法（两层都堵）

1. 纯函数自己扛住：`_offline_row()` 同时接受 dict 与 `(kind, reason)`，认不出的形状跳过，
   不让一条脏数据拖垮整轮。
2. 调用方保留 `isinstance(item, dict)` 防线（experience-engineer 加的）。

### 必须遵守

**测拼接点，不要只测两侧。** 每个跨模块的入参形状，至少要有一条测试用**调用方真实传的东西**
去喂被调方。本次补的四条在 `tests/test_capability_cognition.py::PromptWiring`，
其中一条直接拿真实 `offline_reasons()` 的输出去喂。
真机验证脚本：`.review-server/t3_join_check.sh`（真 Companion + 真 sqlite + 模型打桩，真调 `_messages`）。

---

## 6.1 附注：清理未完成改动，避免跟着修复上线

部署合并版本时发现 `companion.py` 里有一句 `await self.bilibili_reader.startup_sweep()`，
但 `companion_bilibili.py` **没有这个方法**（git HEAD 里也没有）。上线后 00:20:17 报了一次
`AttributeError: 'BilibiliReader' object has no attribute 'startup_sweep'`，
被 `try/except` 兜住、只记日志、没挡启动。

**已从部署版本中移除**——未完成的改动不该搭着别人的修复一起上线。
该功能的实现者补上 `startup_sweep` 后再单独上。

**另：云端 `companion.py` 曾被换回旧版本**（少了 `isinstance` 防风、少了 bilibili 清理段、
`experience` 别名不同）。已用合并后的版本覆盖，两侧改动都在。
**并发改同一个文件前先 `scp` 拉最新**，否则一同步就覆盖别人的工作。

---

## 7. 回滚方法

```bash
cd /opt/qq-deepseek
cp life.bak-t3-20261117-235020/*.py life/
systemctl restart companion.service
systemctl is-active companion.service && curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8765/alive
```

回滚不碰 `life.db`、`config.toml`、人格文件——本轮所有改动都在 `life/*.py` 与 `tests/`。
