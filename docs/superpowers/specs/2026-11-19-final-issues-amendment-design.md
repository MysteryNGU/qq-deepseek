# 最终问题清单：云端核对与修改设计

日期：2026-11-19；状态：建议方案，未实施。补充此前 open-issues-resolution-design，本文纠正清单中不成立的判断，重排实施顺序。

## 1. 基线与证据范围

本轮SSH只读核对：云端 `/opt/qq-deepseek` HEAD `5f620389d4ebcdad365891903c39e80b934faa90`，服务active，未跟踪2500/skills。下载的代码在本地 `.review-server/final-issues-20261119/`。本地life_recall、memory_index、companion、companion_model、companion_context与云端不同，实现必须从云端基线建立分支，不用当前本地文件直接覆盖生产。

核对了代码、数据库聚合和本地隔离复现；未发真实聊天、未跑全量semantic压测、未重启或更改生产配置。清单中的时延、扫描耗时、浏览器峰值仍是原报告数据，不是本轮复测。

## 2. 必须先纠正的结论

| 清单结论 | 当前证据与修正 |
|---|---|
| context(800)无论是否命中必然失败 | 有剩余结果需要分页时，游标可能挤掉内容；不是所有查询必然失败。本地临时空库调用云端LifeRecall类，budget=800正常返回，estimated_tokens=92、cursor=None。该估算不是模型实际token用量 |
| 找记忆从来没工作过 | 该context分支存在严重预算问题，但_messages还有prior_users、history、8000预算的prefetch；不能推出全部记忆失效 |
| ignore_override诊断已解决 | search_async读取ignore_mode_override并pop，但随后search再次resolve_mode且不接收绕过标志。隔离实测：环境text，请求semantic+ignore_mode_override=True，最终diagnostics.mode仍是text |
| text完全不调用search_vectors | search仍无条件调用此函数，只因query_blob为空而快速返回。应保证不生成query embedding、不执行向量SQL；最好也显式跳过整个分支 |
| 日志轮转，原始返回丢失 | _call_model已通过_audit把content、tool_calls、request_id写到companion_activity。此次滚动24小时808条decision均有文本content，17条最终校验错误；尚未逐条关联，不能承诺所有失败都可完整恢复 |
| recall统计只缺真实分母 | 还有字段契约错误：search写route_counts/route_status，prefetch读routes，得到空字典；route_counts的键实际是resource_ref，值是匹配路线数，并非每路线命中数 |
| semantic_items代表装入上下文 | 当前字段数的是prefetch候选；ops/scripts/recall_counts.py据此判断装入，且把semantic_unavailable算作运行成功，均需修正 |
| 空ACL是否意味着全部允许待决定 | 当前代码已经定义：没有显式规则时仍走scope、同人群事件、internal、diagnostic、ancestry判断；不等于全允许。无须重新设计ACL才能优化 |

上一轮提出的生产override入口、非dialogue先清空状态已经修复，不重复派活；上表诊断绕过是另一条未接通路径。

补充隔离复现：短scope/agent绑定下，8条短事件查询电影，budget=800也能返回1条、estimated_tokens=744且有cursor。说明1162只是原实验绑定及packet的开销，不是函数通用下界。此复现采用云端LifeRecall类与本地临时库依赖，用于反证“必然”，不代替生产场景复测；P0仍需按真实scope长度重现并修复。

## 3. 方案选择

不推荐只提高context预算和scan_limit：前者继续为分页付费，后者增加工作量且不能修复事件循环阻塞。也不建议把schema、数据库、浏览器一次全部重写，验收无法归因。

推荐分批修复：轻量context与准确观测先上线；召回读通路和候选排序随后切换；schema先恢复证据再改协议；浏览器独立交付。沿用此前已选的局部改造路线，保留现有业务权限和能力。

## 4. 第一批：context不生成分页游标（P0）

位置：life_recall.py::search/context、companion.py::_messages、companion_town.py、companion_recollection.py、continuity.py::context、runtime.py::context_block。

五个调用方都只消费当次文本：continuity返回memory字符串，runtime拼接字符串，均未使用cursor。因此统一由context选择非分页路径即可，不逐个把800改大。

接口建议：search新增 `include_cursor: bool = True`；context传False。默认分页API保留原行为。False时拒绝传入cursor，不调用_cursor、不写life_recall_snapshot；返回next_cursor=None，has_more/budget_truncated如实反映尚未装下的已授权候选。has_more只表示还有结果，不保证此请求可续页；工具分页仍用默认模式。

预算必须在packet组装及回退循环之前使用同一非分页策略，不能在生成游标后pop掉字段。首次继续按完整非分页packet计预算，context输出的JSON行再校验不超预算，避免新增一套排序/授权实现。预算为0应在context入口直接返回；小预算容不下引用时返回空并记budget_exhausted，不能误报no_match。

保留现有“正文放不下就返回完整引用、preview=None”的语义，不随意截断否定句。不保证800对所有资源都有正文；验收用明确可装下的短记忆，同时覆盖长引用降级。

验收：有短命中/无命中/多个待续结果/超长正文/预算0与极小值/排除refs/跨scope/撤销；检查context路径没有新增快照；五个调用方各测一次。分页API另测续页、去重、失效和预算，不能用context修复改变分页默认契约。

## 5. 同批补齐观测与诊断（P6/P9/P10及新发现）

### 模式

统一公共参数名ignore_mode_override，仅内部诊断可用。search_async和search共享一次已解析的effective_mode；通过私有执行函数接收解析后的模式，或明确传递绕过标志，禁止异步解析完被下一层重写。工具schema不暴露生产绕过开关。

断言四类：普通入口受环境覆盖；诊断入口可绕过；text无embedding和向量SQL；诊断semantic生成向量并真的采用semantic路线。不要用只测试resolve_mode的六个样例替代整条调用链测试。

### 计数

统一一个routes对象，分别含text/semantic/related/exact的attempted、status、candidate_count、returned_count、loaded_count。status明确区分skipped_mode、unavailable、success、partial、timeout、error。成功运行与找到匹配分开统计。

候选、返回、装入是三个阶段：loaded_count在最后一次上下文预算裁剪完成后按实际material refs与match_routes计算。兼容现有recall slot，但增加trace_version，查询脚本不能把旧字段推断成新口径；历史无法重建则记unknown。字段不能包含未授权候选ID或其数量；内部性能总扫描数单独作为受限运维指标。

effective_mode与started在任务启动前写入，total_ms在finally记录；超时仍能知道请求模式和耗时。合资格dialogue总数、semantic实际attempted数分别作为分母；text跳过不算失败。修复脚本对semantic_unavailable和semantic_items的误判。

### 失败证据

先按source、purpose、时间和已有request_id关联companion_activity中的decision与error，找回尚可恢复的历史样本；无法唯一关联就标uncertain，不猜。

新增decision_attempt_id，每次初始及纠正调用各一个，贯穿原始返回、解析、业务校验、最终错误。记录schema_version/hash、provider/model、finish_reason、validator及JSON路径。原始返回已有持久审计，先复用，不必立刻复制一套artifact系统。若改成artifact，必须记录引用、保存成功状态、清理期限与删除结果；数据库及备份一同遵守私聊隔离和遗忘要求，运维报告不输出正文。

## 6. 第二批：召回性能与scan_limit（P1/P2/P3/P4/P7）

保留RECALL_MODE_OVERRIDE=text，直到真实dialogue和混合负载验收通过。不能只凭scan_limit分支修好就撤掉保护。

scan_limit保留已扫描结果可作为局部容错修复，但不能直接写内层break：必须退出整个资源+chunk扫描，返回已排序top-K并标partial及scan_limit原因。该前缀按现有资源顺序，具有近期偏差；不构成完整语义召回验收，也不解决时延。若不准备单独交付这项容错，可直接在v2实现同一截断语义。不要根据28秒线性外推七万片的性能。

正式路线沿用上一份设计：各路线先候选ID与分数，再精确授权，最后加载正文；批量向量读取/紧凑数值表示，保留model、chunker、source_version过滤及旧记忆可达性。不能以词法命中作为semantic的必需候选，否则同义不同词仍然找不到。授权不足继续扩展候选，不让未授权top-K挤掉可见结果。

独立只读worker必须覆盖所有认知路径的长召回，不只是_prefetch_recall。当前_messages还同步context，非对话还会recollection.pick；town、continuity、runtime也需枚举调用。对认知入口先异步备好RecallPacket，_messages仅装配，不再现场查全库；其他同步API保留同步契约但从事件循环调用时移到对应worker。不要在async函数中对Future.result()阻塞。

LifeRecall.__init__本身会写游标签名key；只读session不能直接用旧构造器。启动期由主写者准备key，读服务注入只读配置。search分页快照写入及access.load默认track=True的副作用也要拆出：只读检索禁用写操作，由主写者在最终选材和授权复核后、内容交给模型前登记会话读取。登记与最终权限版本检查使用受控短事务；权限已变则重新校验或丢弃，不能留下“模型已使用、撤销逻辑却未登记该读者”的窗口。此登记不代表模型一定调用成功，实际调用另记结果。

单worker、一运行一待处理、合作取消、短事务和最终授权复核沿用原设计。局部ACL缓存只缓存规则查询，不变更分支；一致快照内空表只省SQL。最终权限校验也必须有界，不能把全量扫描移到“最终校验”。

验收不要求MainThread空闲率必达30%；这个比例受负载与采样影响，只作定位证据。正式看同一负载下/alive p95≤100ms目标、事件循环延迟、聊天端到端时延、召回p95、峰值内存、取消后工作是否停止及队列上限。超标需定位其他同步热点，不能声称召回worker必然解决全部DB卡顿。

## 7. golden应分成两类

云端实际存在两份文件：ops/oracle/golden_recall_v1.json（30302字节）与tests/golden_recall_20261119.json（18586字节）。辅助文档明确前者INCONCLUSIVE；实施先核对两份的生成条件和版本，不能混用报告与不同文件。原数据库副本据交付文档已删除，重新备份的数据不能与旧JSON逐项硬比。

1. 兼容集：同一新隔离副本、固定时钟/权限/索引版本，新旧代码双跑；比较不受修复影响的text排序、refs、来源和权限结果。签名游标、生成时间、随机ID不能逐字比较。对预算变化用足够预算的候选排序对比，包装另测。
2. 修复集：P0从报错到有内容、scan_limit从丢弃到partial、新semantic/related命中都明确允许改变。建立人工可解释的合成用例：无词法重叠但向量相关、实体关系、多scope deny与撤销、未覆盖索引等；要求对应路线真实参与。无结果是合法用例，不用整体非空率80%作正确性门槛。

单独列出差异原因；“新旧全字段全顺序相同”不能作为修复已知错误的上线前提。

## 8. schema决策（P5）

strict_schema确实把属性全部设为required，但不能由13次失败推出“27个可选字段导致全部失败”。DirectModel记录wire_schema_error，上层_decode_decision另按原始DECISION校验，并非每个wire错误都会终止一轮。本轮24小时301条decision有wire_schema_error，17条最终校验错误，两个口径不可混用，更不能据此计算同一个失败率。

第一步完成第5节样本关联，区分JSON截断、传输schema、业务schema、状态冲突和执行失败，按实际模型调用/认知轮次分别统计。reply缺失不自动补成成功；多余键不通用删除，避免掩盖模型把意图放错位置。

若样本支持简化协议，推荐保留单次调用，拆开wire schema与内部DECISION：顶层固定reply/actions/updates，其他可选业务变更映射到updates的带kind分支，无变化为[]；明确每个旧字段的映射并验证全能力往返、重复/冲突项拒绝。array items用互斥kind的anyOf，不把所有分支字段摊成一个巨型object。旧协议保留版本解析及回滚，新旧输出均进入原业务守卫。此为后续候选设计，不与P0绑在一起上线。

不先选两次模型调用：增加费用和等待，且尚无证据必须这样做。也不直接假定DeepSeek或中转支持OpenAI的nullable可选编码。DeepSeek官方文档要求object属性全部required、additionalProperties=false并列出anyOf支持；OpenAI文档说明可用null表示可选，但二者兼容性需实际端点契约测试。来源：[DeepSeek Tool Calls](https://api-docs.deepseek.com/guides/tool_calls/)、[OpenAI Function calling](https://developers.openai.com/api/docs/guides/function-calling)。

## 9. 浏览器与索引（P8/P11）

云端companion_executor仅看到browser可用性开关，companion_browser.py不存在，adapter草稿存在；不能把DECISION枚举已有browser当成链路接通。继续原云端设计：预配置独立companion-browser.service、Unix socket、任务租约、结构化回执；登记executor实际分派、host健康与可用性同源。A阶段验收前状态为未接通，不能宣称可用。

浏览器可与召回独立开发，但发布前必须在浏览器+召回+embedding同时负载下验收内存与交互。环境安装不重做。索引继续按当前版本覆盖、unknown分类和有界重试验收，不以pending归零代替成功，不必等全量完成再修context；v1在替代覆盖和回滚验证后清理。

## 10. 建议交付顺序

1. P0非分页context＋模式诊断修复＋统计契约修复；每项独立回归，作为第一批交付。
2. 同时恢复P6审计关联，建立schema失败分类；保持text生产保护。
3. worker完整调用链迁移＋候选优先v2，按兼容集与修复集验收，再做真实dialogue与混合负载切换。
4. 浏览器A独立推进；索引调度与覆盖核对持续进行。
5. 有失败样本依据后再决定schema v2试点，不先整体拆成两次调用。

每批交付独立代码hash、测试证据、剩余问题与恢复方式。实施前再次核对云端HEAD及运行进程版本，避免本地旧代码覆盖生产；最终STAMP在所有修改结束后生成。
