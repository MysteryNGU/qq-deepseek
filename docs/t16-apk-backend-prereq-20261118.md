# t16（P35）APK 后端前置：历史分页 / 任务产物 / 生活条目 id / 版本号

日期：2026-11-18 10:40–11:05 CST　执行：isolation-verifier
范围：**只改服务端**，未碰前端。改完重启 + 真实 HTTP 验证。

## 0. 部署与版本

```
DECLARED 238e02e28c82510849b851258b6cb361  ACTUAL 238e02e28c82510849b851258b6cb361  MATCH life/companion.py
DECLARED fb4a78b02384ef3b020bb6eb0aef580f  ACTUAL fb4a78b02384ef3b020bb6eb0aef580f  MATCH life/companion_mobile.py
DECLARED 558172104eeca1e4c37013b674f3f3c3  ACTUAL 558172104eeca1e4c37013b674f3f3c3  MATCH life/companion_remote.py
```

- 备份：`life/{companion,companion_mobile,companion_remote}.py.bak-t16-20261118-105419`
- 真实 import（不是 py_compile）：`IMPORT_OK`，三个模块的新符号全部就位
- 重启后：`active`、`NRestarts=0`、`/alive=200`、err 级日志 0 条

---

## 1. 历史分页（APK 需求 3）

### 做法：两处限制都处理，游标用 `ingest_seq`

| 位置 | 改动 |
|---|---|
| `life/companion.py:_recent` | 新增 `before=None`；SQL 加 `AND e.ingest_seq < ?`；SELECT 多取 `e.ingest_seq` |
| `life/companion.py:snapshot` | 新增 `history_limit=None, history_before=None`，**只在传了才生效** |
| `life/companion_remote.py:CompanionServer.snapshot` | 透传分页参数；**带分页参数的那一页不进 2 秒缓存**（否则首屏会串成旧页）；老 core 无此参数时不传 |
| `life/companion_mobile.py:state` | 解析 `?before=`、`?limit=`；非法值 400；只在该传时才向上游要分页快照 |
| `life/companion_mobile.py:mobile_view` | 透出 `history_before`（下一页游标）、`history_limit` |

**为什么用 `ingest_seq` 而不是时间戳**：消息可能同一秒到大，按 `received_at < before` 会把同秒的整批漏掉或重复。
`ingest_seq` 单调、唯一，天然是稳定游标。前端**不需要知道它是什么**，把响应里的 `history_before`
原样回传即可；每条消息也带自己的 `cursor`。

### 实测（真实 HTTP，`/mobile/state`）

```
page1  http=200 bytes=75430   count=60  before=10664  first=evt_15ae5f46… last=evt_7abce56c…
page2  http=200               count=60  overlap=0    next_before=10581
       page2 的每一条都早于 page1 的头部：True
```

- 第 61–120 条确实拿到（`limit=200` 也能一次取 200，说明上游 100 的上限已解开）；
- `overlap=0`：两页不重叠，游标链 `10664 → 10581` 连续；
- **不传参数时行为不变**：`history[0]` 仍是原来的字段集，只在末尾多了 `cursor`，
  顶层多了 `history_before` / `history_limit` 两个**新增**键，原有键一个没少、没改语义。

---

## 2. 任务结果与产物（APK 需求 7）

新接口 `GET /mobile/tasks`。

**关键设计：`status` 和 `verification` 是两件事，接口不合并。**

```json
{
  "id": "companion-agent_f9c833556fb8cb4809…",
  "status": "completed",          // 它说做完了
  "verification": "pending",      // 还没有证据
  "claimed_done": true,           // status=='completed'
  "verified": false,              // verification=='passed'
  "blocked": {"code": ..., "reason": ...} | null,
  "summary": "…", "outcome": "completed",
  "artifacts": [{"id","name","path","bytes","readable","sha256"}],
  "note": "status=completed 表示它说做完了；verified 才是核验通过。"
}
```

`claimed_done` / `verified` 两个布尔是给前端**故意冗余**的：前端做「✓ 已完成」徽章时
必须用 `verified`，用 `claimed_done` 就是把「声称」画成「完成」。

实测（生产，2 条真实任务）：

```
companion-agent_f9c83355… status=completed  verification=pending  claimed_done=True  verified=False
companion-agent_3762ab54… status=interrupted verification=pending  claimed_done=False verified=False
```
生产上那条「已完成但 pending」正好落在设计点上：**它跑完了，但没有留下可判定的证据**，
接口如实显示 `verified=false`，不是 bug。

**产物可读性**：`readable` 只报「文件在不在、是不是在 home 里」，不新开一条把任意文件吐给手机的通道。
隔离测试里两个产物一个在 home 内（`readable=true`）、一个指向 `/etc/hostname`（`readable=false`），
判定正确。

---

## 3. 生活条目 id（APK 需求 6）

生活条目本来就带 `id`（`companion_life_view._item` 里 `item=dict(id=row['id'], …)`），
**缺的是手机视图把它裁掉了**——`LIFE_ITEM_FIELDS` 白名单里没有 `id`。

改动：`LIFE_ITEM_FIELDS` 加 `id / kind / source_ids / source_ref / age`。
其中 `source_ids` 是同一段经历被合并的几条（生活视图本来就带），前端可以整段引用。

实测（生产）：

```
today_preview  n=12  ids=['bd3d1818…', '81bc054e…', '1cac4a67…']
past_preview   n=20  ids=['36bc45c4…', 'a292c10c…', 'd4d2c86b…']
ideas_changes  n=6   ids=['d84e96b8…', '036eaecf…', '5a6e2eb6…']
recollection   id=28f44aa5…
latest_letter  id=None（当前没有写信）
```

**前端怎么带这个 id 继续聊天**：它已经在本轮 `source_ref` 体系里，前端只需
`POST /mobile/chat` 时把 id 作为一个引用一并带上（`input_source.recollection_ref` 那条链）。
这条**我没有改 `/mobile/chat`**，因为 user 侧要求「只改服务端、给前端数据」，
而引用参数的接入方式属于前端协议决定——**需要前端确认它要哪种形状**，我再补。

---

## 4. 应用更新提示（APK 需求 8）

新接口 `GET /mobile/version`：

```json
{"ok": true, "version": "0.4.7", "apk": "Xiaoyu-0.4.7.apk",
 "bytes": 176939, "url": "/downloads/Xiaoyu-0.4.7.apk",
 "folder": "/var/www/companion/downloads"}
```

版本号**从发布目录里真实存在的最新 apk 文件名推出来**（`Xiaoyu-<x.y.z>.apk`，按语义版本比大小），
读不到就返回 `version: null` + 原因——**绝不编一个版本号**，否则每台手机会提示一次假更新。

---

## 5. 隔离验证（部署前，同机跑真代码）

`.acceptance-local/t16/verify_p35.py`，容器 = 部署的那份 `life/`，临时 HOME，真 `Companion`
+ 真 `CompanionServer.snapshot` + 真 `MobileAPI`：

```
P1 page1=60 before=81  page2=60 overlap=0 next=21  limit200=140   paging_ok=True
P2 status=completed verification=pending claimed=True verified=False artifacts=[art-1 read=True, art-2 read=False]
P3 today=3 ids …  recollection=act-probe-recollection  letter=act-probe-letter  all_have_id=True
P4 version=0.4.10 (取最大语义版本)  missing→version=null+reason  routes=/mobile/version,/mobile/tasks
```

P3 那段**先失败过两次**，原因值得记：我第一次只插了 `companion_activity` 行，
但没满足生活视图自己的 `LIVED_SQL`（要求 `result.status ∈ 成功/失败集合` 且 `completion` 不是 accepted/queued），
于是投影把它们全部过滤，`today_preview` 是空数组——**空数组会让「所有条目都有 id」恒真**，
那就是一个假通过。改成满足 LIVED_SQL、且文案是人话（否则会被 `technical` 过滤），才真的验到。

---

## 6. 过程中的事故与处理（如实）

1. **我一度把探针 token 做成了非法前缀**（`t16probe…` 而不是 `mob_…`），第一次 HTTP 全部 401。
   改成 `mob_` 前缀后通过；这暴露了一个事实：**服务启动时把 `mobile-devices.json` 读进内存**，
   新写的设备要重启才生效——前端排障时有用。
2. **探针设备已撤回**：`mobile-devices.json` 里只剩原来的 2 台「小鱼安卓」，
   对比备份确认「移除的只有 `t16-probe-device`，原有设备一个没少」。token 文件已删。
3. 为让 token 生效我重启了两次服务；最后一次让服务在**干净状态**（无探针设备）运行，
   `active` / `/alive=200` / 0 err。

---

## 7. 未覆盖 / 需要别人接的

1. **前端带 id 继续聊天的参数形状**没有定（见 §3），需要前端确认后再补 `/mobile/chat` 的入参。
2. `/mobile/tasks` 里 `projects` 一次最多 40 条，没做分页——目前生产只有 40 条以内；
   如果项目数会涨，需要像 history 一样加游标。
3. `verification` 的 `passed` 分支在生产上**当前没有样本**（现有两条都是 pending），
   所以「verified=true 时前端长什么样」只有隔离测试覆盖，需要以后有成功核验再复核一次。
