# 新版记忆运行与恢复

本机管理入口为 `scripts/memory_admin.py`，命令通过 `data/memory-admin` 的持久请求和回执，由正在运行的 Bot 串行执行。它没有远程监听端口，也没有模型工具入口。原始事件始终留在 `events`；管理回执只保存编号、状态、费用证据，不复制私聊正文。超时只代表本地回执尚未读到，应按输出的 request_id 查原回执，不能重复提交付费动作。

```powershell
.venv/Scripts/python.exe -X utf8 scripts/memory_admin.py status
.venv/Scripts/python.exe -X utf8 scripts/memory_admin.py reconcile --target <job_id>
.venv/Scripts/python.exe -X utf8 scripts/memory_admin.py pause --target <job_id>
.venv/Scripts/python.exe -X utf8 scripts/memory_admin.py retry --target <job_id>
.venv/Scripts/python.exe -X utf8 scripts/memory_admin.py repair --target <event_id>
.venv/Scripts/python.exe -X utf8 scripts/memory_admin.py verify-history
.venv/Scripts/python.exe -X utf8 scripts/memory_admin.py verify-qq
```

- `reconcile` 只查询 SDK 的原输入状态及实际用量，不补发输入。
- `pause` 用于受控维护。cancel_requested只返回stopping，继续保留租约和费用；收到stopped后才把作业置为paused/operator_paused，并保留原检查点。它不会释放未结费用或立即发起新一轮。
- `retry` 仅处理 paused/quarantined。须确认旧执行 stopped、原输入 settled/rejected/not_found、原调用费用可核对，并通过当前用途/来源和预算检查；正在运行、真正 unknown、未结费用或仅 cancel_requested 均不能重发。通过后创建独立、可审计的新整理作业，旧作业保留为 cancelled/operator_replaced。已完成覆盖不回退。
- `repair` 用于已记录的 waiting_repair 或用途排除项。先完成真实投影/权限修复，再在当前权限下重新读取原事件并创建独立整理作业。不清除学习 barrier、不绕过遗忘、不重写原扫描清单或此前摘要的覆盖结论，也不回退小时游标。
- 请求已标记开始但 Bot 在写回执前退出时，返回 operator_outcome_unknown；同 request_id 不自动执行第二次。先核对原作业、SDK 回执与账本。
- `verify-history` 在现行学习 floor 之前的合法旧用户原话中抽样，只返回可读性、学习资格及编号，不输出正文。历史可读不等于学习资格恢复。
- `verify-qq` 通过正式 Expressions/Delivery 链发送一条固定验收通知到主人私聊，并保留维护观察及真实送达回执；它不伪造真人入站。

外部收费计算迟到账单可使用 `import-bill --target <call_key> --bill <file.json>`。证据需有真实供应商、invoice_id、与预留一致的 price_version、actual_nano 和 finished_at。冲突账单不能覆盖已结算记录；缺证据、缺用量或超时不释放预留。不把人工估计或供应商未报告的费用记为零。账单按原完成日计入，不能通过导入时刻改变归属日。

内部整理有独立的同进程续租心跳，不依赖慢回执读取返回后才续租。它不做模型调用，不认领已过期工作，不延长会话总超时，不复活退役会话或跨越政策/遗忘 epoch。进程退出后仍按原租约和回执恢复，不因心跳机制放松 unknown 保护。

内部分页的当前位置由宿主按作业持久保存。模型继续调用 `life_read(proposal:{kind:memory_batch})` 即可从最后确认的分页位置接续，`next_read`也提供同样参数；不再要求模型搬运长签名字符串。显式分页编号使用当前批次短句柄，宿主再验证其背后的签名游标、来源/会话/权限和期限。错误编号仍拒绝；同一call_id重放原回执，不重复推进。读完后再次读取返回空页和has_more:false，不能把未读项计为完整覆盖。

## 本机中文向量服务

用户已授权本机下载及部署。当前使用 BAAI/bge-small-zh-v1.5，固定 revision `7999e1d3359715c523056ef9478215996d62a620`，官方 safetensors SHA-256 为 `354763b9b1357bc9c44f62c6be2276321081ed2567773608c0d0785b61d5a026`，MIT 许可。来源为 [BAAI 模型仓库](https://huggingface.co/BAAI/bge-small-zh-v1.5) 与 [BGE 官方说明](https://bge-model.com/bge/bge_v1_v1.5.html)。

```powershell
.venv/Scripts/python.exe -X utf8 scripts/install_local_embedding.py
.venv/Scripts/python.exe -X utf8 scripts/evaluate_memory_service.py --existing
```

安装程序固定版本并验证完整权重摘要，不激活部分下载。启动只读取本地 safetensors、tokenizer 和 manifest，禁止远程模型代码和隐式联网下载。服务固定监听 `http://127.0.0.1:8231/v1/embeddings`，CPU、2线程、512维、CLS/L2归一化、不加查询指令。请求最多32条、262144字节；超过模型512 token的单条输入拒绝处理，不偷偷截断原始经历。当前索引使用240估计token/32重叠，正文仍由统一事实库提供。模型、提供方、分块及 manifest 的改变会更换索引代次。

`--existing`只连接并核对正在运行的端点，不启动或停止生产服务；未运行Bot时可省略该参数，由验收脚本临时拥有服务并在结束时关闭。QQ需重新登录时，本机模型仍可独立启动；它不依赖OneBot连上后才加载。

Bot 启动和关闭管理服务的进程树；运行中服务退出会尝试重启。只接受本进程树内且 manifest 摘要吻合的健康回执，不把端口上另一个服务误认作自己的模型。127请求显式绕过系统代理，文本不会因系统代理设置而转发到外部。

本地 CPU 服务无供应商调用费，账本使用 `local-cpu-zero-provider-fee-v1` 和单价0，但仍保存真实 token、代次与结算回执；这不度量电费。已知成功缓存淘汰后产生新代次；真正 unknown 的结果仍不能自动重发。此前无关的未知费用不会因启用本地服务而清空。

合成中文验收共8篇材料、24个问题，预先门槛 recall@1≥0.8、recall@3≥0.95；本次实测均为1.0。另有24条中文分享宿主规则反例全部通过。结果见 `data/maintenance/memory-r1-r7-production/local-evaluation.json`；它是有限语料验收，不是所有真实中文交流的准确率，也不是模型普通分享分类准确率。全量历史索引为后台增量工作，verified只表示端点合格响应，不表示所有历史已完成索引。

## QQ 内核网络故障与监督进程

2026-10-26的`1006514 / 网络连接异常`来自NTQQ sendMsg的原生拒绝回执。它与OneBot WebSocket连接成功不同，也与等待onMsgInfoListUpdate超时不同。前者记录failed及脱敏的原生错误码，不写共同经历；后者仍为unknown，所有发送入口都要求先对账，不因API包装成retcode=1200而误判未发送。

发送前只读检查get_status；明确离线或检查失败时可证明尚未发出实际发送请求，记录deferred。原生网络拒绝后保持30秒持久冷却，重启Bot不清空冷却；只对实际带message_id的成功回执登记确认。get_status可能显示旧在线状态，因此它不能单独证明发送能力。复核只给角色一个带原归属的观察事件，不自动重发旧草稿；首次启动招呼失败不再阻塞复核，复核文字也不未经证据宣称网络已经恢复。

QQ凭据失效属于登录授权：恢复原账号，不切换到其他缓存账号，不删除QQ数据。优先使用原账号真实快捷登录凭据；不存在时需手机扫码确认。当前排查使用NapCat现有set_restart及WebUI接口，原机器人尾号1941不在快捷登录列表，已要求重新扫码。不能将WebUI在线、端口可连或get_status的online:true写成QQ收发已经验收。

Windows健康文件偶发共享锁时，监督器仅可复用上一次已认证的心跳，并重新检查原20秒有效期、token及进程树归属；无有效样本、过期或身份不符仍失败，不关闭监督保护。SDK在turn/end之后附加session/end-seed时，也必须识别轮次已经结束；未提交摘要的内部作业暂停并报原因，不继续假称运行中。

登录后初始化的宽限从本进程首次连接QQ开始，且仍受既定启动时限约束，不能把等扫码的时间当作认知初始化超时。通知积压时每条持久事件处理后让出事件循环，来源依赖批量登记为一次事务，已经登记的依赖不重复写入；这样仍先保存读取依赖再返回来源材料，不放宽权限或遗忘传播。

内部整理分页同时遵守SDK的预览阈值与硬上限，给游标/结果结构预留空间，避免宿主说已读完、模型却只看到截断预览。内部状态提供本批完整source_refs，coverage不足与格式错误使用各自的明确提示。异常结束和主动暂停保留分页检查点。摘要按主题简短归纳，重复或无法可靠归纳的来源明确列为omitted；这不删除原文，也不把部分摘要标成完整覆盖。

模型因max-tokens结束同样是未提交，不是成功；生产将其暂停，保留检查点及费用。恢复前先确认执行停止、输入及真实usage已对账，再由管理员明确retry；不自动提高费用上限或循环重发。有限反例和实际批次验收不保证任意长材料都能在现有输出/时间限制内一次生成摘要，后续容量与批次参数仍须据实测调优。
