# B站插件审计与独立收件箱（2026-11-15）

本次先阅读原插件 AGENTS.md，再对照实际代码检查。没有修改原插件，没有测试发评论、私信或点赞，也没有打印凭据。

## 原插件实际提供的省 token 机制

- `index.js:562` 的 `scanWith` 调用 DSH `subagents.startActivation(provider='spawn', delivery='caller')`。完整搜索、字幕、评论进入子代理会话，主角色只收到它的报告与投递提示。子模型 provider/model 都有值才覆盖，否则沿用父会话。
- `vendor/bilibili-mcp/mcp_server.py:392` 的 `bili_subtitle` 读取第一分P的 cid，优先 AI 中文字幕，下载所有段落并拼成完整 `text`。它不调用摘要模型、不分块、不缓存、也不读取视频画面；没有字幕会明确返回没有字幕。子代理报告才是压缩层。
- 报告可 full/brief/off；off 仍要求子代理交简短总结，只是不写详细日报。`reports/<BV>-<日期>.md` 是按 BV 写出的笔记，`index.js` 的读取用途是界面卡片；没有发现 `scanWith` 根据这些文件跳过字幕或复用摘要的实现。
- `seen.md` 在宿主启动时仅预建。当前扫描路径没有程序化读取它并阻止重复扫描的逻辑，不能把这份文件称作生效的视频摘要缓存。
- 搜索工具实际做播放量/发布时间硬过滤；配置门槛时会多抓最多4倍后筛选。过滤减少子代理需要读的候选，但不是字幕压缩。
- 本机插件磁盘配置为 subProvider/subModel 空、perScan=1、reportLevel=full、notesOn=true、watchOn=false。DSH原生设置可逐字段覆盖磁盘配置，因此这些不是当前宿主生效值的证明。

## 原收件箱值得复用及需要修正的地方

`index.js:1231` 起收件箱将私信、@、回复归一化，保留 BV、发送者、rpid/root 等回复路线。`inbox.json` 和 `watch.json` 记真实消息ID；先成功交给角色，再记递送。私信转发来源另记6小时，避免把私信回复错发到公开评论区。

`index.js:1703` 起 watcher 每120秒（配置60–1800）先读 `bili_unread_messages`，有未读才拉具体内容，空轮不调用模型。代码的条件是未读数大于0，**不是 README 所说的“未读数变了才读”**。旧未读数不清零时仍会重复拉取，后续靠ID账本去重。

不能原样复制的缺口：

- watcher 将最终通知截成6条，却在投递后给完整的私信/提醒数组记已递送，超过6条的余项可能从未给角色看就丢掉。
- 旧watch首次开启没有历史基线，已有未读可能被当成新消息交给角色。
- watcher使用单独私信账本，普通pullInbox的DM使用另一份账本；不能据README认定所有私信路径都共享去重。
- 原提示词强烈面向“把报告递给主人/评论@主人/私信主人”。这不适合作为独立B站生活的默认动作；需要由独立B站上下文决定何时互动、何时值得主动分享。

## 独立化后原本遗漏了什么

原先独立 `life/companion_bilibili.py` 只迁入按需启动的 Python MCP。DSH层的 `bili_scan` 子代理、定时扫描、watch/inbox状态机、按BV笔记、投递来源编排没有随它迁入。原独立工具输出是原始工具文本截取，不能称作子代理观看报告。新的视频读取器由并行任务负责，本文件不修改其实现。

## 云端真实探针

2026-11-15 使用现有云端 Python 环境启动现有 MCP，仅执行 initialize、tools/list、bili_check_credential、bili_unread_messages，然后关闭探针子进程：

- 后端可启动，实际注册30个工具，**没有 bili_scan**。
- `/var/lib/companion/xiaoyu/data/companion/bilibili/bili_credential.json` 不存在。
- `bili_check_credential.logged_in=false`；未读工具返回 MCP 未登录错误。当前仍需云端扫码，不能宣称已能接收B站消息。
- 原 `bili_check_credential` 通过公开用户资料接口判断状态，单独返回true不足以证明私信鉴权有效。新helper还要求真实未读接口成功才进入online。

实际输入参数：

| 工具 | 必需参数 | 可选参数与默认值 |
|---|---|---|
| bili_search | keyword:string | num:int=10, order:string=totalrank |
| bili_subtitle / bili_video_info | bvid:string | 无 |
| bili_comments | bvid:string | num:int=30 |
| bili_dm_sessions | 无 | size:int=10 |
| bili_dm_recent | 无 | size:int=20, talker_id:int=0, pages:int=1 |
| bili_unread_messages / bili_received_replies / bili_received_at_and_likes | 无 | 无 |
| bili_reply | bvid:string, text:string | rpid:int=0, root:int=0 |
| bili_send_message | receiver_uid:int, text:string | 无 |
| bili_triple | bvid:string | mode:string=like, coin_num:int=1 |

## 新的独立收件箱 helper

`life/companion_bilibili_social.py` 提供 `BilibiliSocial(core)`。默认从当前 `core.executor` 沿 `original` 链动态取 `_bilibili`，不启动DSH、不创建第二个角色、不自行调用模型或发送消息。

- async `start()`、`set_active(active)`、`close()`；`poll_once(force=False)`用于检查和测试。
- sync `context()`/`snapshot()`只给连接状态、计数、轮询时间，不把来信正文放进owner聊天上下文。
- `pending(limit=1)` 返回 `{id,text,source,source_event_id,status,occurred_at}` 列表。
- `claim(id)` 原子地 pending→working，返回item或None。
- `settle(id,result)` 仅表示该收件项处理结束，不代表外部发送成功。completed/其他结束状态→processed，failed→failed，deferred/retry配after_seconds→延后pending。

收件来源为独立 `bilibili:social` scope，`source_kind=tool_observation`，`actor_id=bilibili:<真实发送者UID>`；source带 `kind/channel/sender_uid/recipient_uid/thread/message_id/bvid/rpid/root/nickname`。消息正文即使自称主人也不会获得owner身份。主调度器负责公共人格、来源隔离和精确外发路线。

持久队列与流基线放在现有life.db。首次成功读取的真实ID仅记baseline；失败通道不会提前宣称初始化。所有实际拉到的新项先完整入队，再限制单次pending/claim数量，避免丢弃批次余项。DM每次读取30条×3页；底层接口只提供最近会话页及@/回复页，没有实现无限历史补抓，不能保证长时间离线后超出这些页面的消息完整恢复。

轮询正常120秒。空未读只请求一次未读数，无模型调用。未登录900秒等待；每30秒只检查本机凭据文件元数据，扫码落盘后提前重新检查，不读取或输出凭据内容。其他失败指数退避，最大1800秒。进程中断的working项标interrupted，不自动重放可能已成功的外发操作；由主调度器按已有动作回执对账后settle。

验证：`python -m unittest tests.test_companion_bilibili_social tests.test_companion_bilibili -v`，11项通过，包括真实SQLite事件隔离、历史基线、批次余项、评论别名去重、扫码恢复、暂停与崩溃不重发。没有真实发送测试。
# 收件解析兼容（2026-11-15）

真实云端复核发现部分 `bili_dm_recent` 会话包含 `msg_type=5` 且 `content` 解码为整数。原插件对所有解码结果调用 `.get`，导致整段会话失败。`companion_bilibili_compat.prepare_backend` 仅在独立 MCP 副本的私信正文和会话预览解析处增加非对象守卫；不修改原 DSH 插件。未知标量内容为空，正常文本仍保留，会话条目也不会因最后一条是系统事件而消失。

社交队列进一步忽略纯空/空白内容，避免空事件触发付费认知；状态保留 `error_tool`，已认证账号的局部接口失败显示 `logged_in=true`、`state=error`，不再误报未登录。

临时副本真实只读探针：原先四个会话中两个抛 `AttributeError`，修复后四个均成功（8/5/3/4 条）；未输出消息内容、未发消息、未改运行中服务。login/social/compat 共 19 项测试通过。

