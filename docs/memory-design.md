# 小鲤长期记忆设计：一个 bot、一份记忆、一致人格

日期：2026-10-25　状态：待实施设计　范围：Python / SQLite `data/life.db`

本文按 **8 个逻辑页**组织，供评审和分期实施；Markdown 导出时可在分页标记处分页。本文只提交设计，没有改业务代码、执行迁移或运行测试。

## 第 1 页｜目标、现状与总体决策

### 1.1 必须满足的三条约束

1. **一个 bot 对应一个稳定 `agent_id`，拥有一份统一记忆集合。**聊天、阅读、游戏和群聊都是这一个角色的经历；会话是来源和权限边界，不再是记忆归属边界。两个 QQ bot 必须使用不同 `agent_id`，即使共用数据库也不能互相检索。
2. **人格底色全局一致，态度按真实人物不同。**人格定义价值观、身份、表达底色；relationship 记录关系状态；人物画像记录“我如何理解和对待这个人”。三者不能互相替代。
3. **记得住与能向谁说是两件事。**全部已接收且允许保留的事件先可靠落库，不因摘要价值低而丢弃；每轮只检索与当前话题相关、且允许当前受众看到的部分。显式遗忘、来源撤销和存储故障属于单独处理的生命周期事件。

“公用记忆”在本文指 **bot 内统一存储、统一检索入口**，不是“向所有联系人公开全部私聊”。这项解释同时满足跨会话记忆与可见性矩阵的要求。

### 1.2 本次只读核实到的情况

| 位置 | 当前行为 | 对设计的影响 |
|---|---|---|
| `life/memory.py`：`remember`、`retrieve` | 写入严格检查原会话及受众；检索对来源再校验 | 不能仅删除查询中的 `conversation_id` 条件；来源、写入和引用规则也要兼容统一目录 |
| `life/repo.py`：`search_memories`、`recent_memories`、`find_facts` | SQL 主要按 agent、conversation 过滤；这些查询并未全部在 SQL 层过滤 audience | 新入口必须先做 SQL 权限过滤，再读取正文，不能把后置过滤当唯一屏障 |
| `life/runtime.py`：`context_packet` | 先分配 recollection 预算，再取记忆 | 作为统一上下文组装入口保留，扩展人物画像和预算统筹 |
| `life/role_agent.py`：`context`；`life/continuity.py`：`context` | continuity 开启时清空旧 memory 块，直接调用 `retrieve_multi` | **两条路径都必须接入**，否则改 `context_packet` 后主路径仍不生效 |
| `life/runtime.py`：`state_key`；`life/relationships.py`：`load`、`view` | 关系状态按 `agent_id + actor_id` 存；可引用材料仍按受众筛选 | 保留现有指标体系、政策和变化机制；画像不另建好感分数 |
| `life/recollection.py` | 叮嘱、关联、解释带来源、适用性和有效期 | 保留任务/交流/长期适用范围，不因统一记忆而把临时要求变成全局规则 |
| `life/persona_files.py`：`select` | 主人私聊取 private；群聊取 public；其他私聊优先 `personas/<actor>.md` | 后续撤销按人选人格，并统一现有 private/public 底色 |

未读取整个仓库、真实数据库、既有人格文件、schema 定义或 TMC 实现代码。下文新增接口、DDL 与迁移步骤均是**拟议方案**；未读取模块的改动列为实施前核对项，不声称已确认其内部实现。

### 1.3 借鉴 TMC 的部分与本项目取舍

参考仅为 `data/tmp/plugin-inspect/tmc-0.4.0/tmc/README.md`。保留五区：`l` 聊天、`d` 阅读、`e` 游戏、`f` 群聊、`g` 备用；它们是检索分类，**不是权限**。保留 L1 原始经历 → L2 摘要 → L3 索引与 L4 归档页 → L5 人物理解的思路，但用 SQLite 和来源关系实现，不并行增加一套 JSONL 真相库。

TMC 的 L4 接收上游整页材料，部分路径已压缩；本项目另保留事件原文，避免把摘要归档误称为原始记录。TMC 的人物总结适合借鉴，`user`、群昵称和简称不能直接用作本项目人物主键；一律使用平台确认的 `actor_id`。不照搬其万字常驻层或压缩阈值，先沿用当前 token 资源政策。

<div style="page-break-after: always;"></div>

## 第 2 页｜数据模型：统一目录与可见性

### 2.1 保留原表，添加目录侧表

**不改写现有 `memories.conversation_id`，不把历史私聊改成虚构的 global 会话。**`memories` 继续存摘要、状态、版本、事实字段和来源；`memory_sources`、`memory_terms` 继续使用原 ID。新增 `memory_catalog` 将这些记录纳入 bot 的统一集合。第一阶段不要求改变现有 Memory/Scope 合约。

以下为可实施的初始 DDL 草案，时间统一为 UTC 秒；迁移时应根据实际 schema 检查已有索引和外键。`origin_*` 来自原记录，创建后不得随聊天目标改变。

```sql
CREATE TABLE memory_catalog (
  memory_id TEXT PRIMARY KEY REFERENCES memories(id),
  agent_id TEXT NOT NULL,
  origin_conversation_id TEXT NOT NULL,
  origin_audience_policy_id TEXT NOT NULL,
  zone TEXT NOT NULL CHECK(zone IN ('l','d','e','f','g')),
  subject_actor_id TEXT,                  -- 无法可靠识别则 NULL
  access_class TEXT NOT NULL DEFAULT 'origin'
    CHECK(access_class IN ('origin','granted','public','quarantine')),
  acl_version INTEGER NOT NULL DEFAULT 1,
  catalog_version INTEGER NOT NULL DEFAULT 1,
  indexed_version INTEGER NOT NULL,       -- 对应 memories.version
  indexed_at REAL NOT NULL
);
CREATE INDEX ix_mc_agent_zone
  ON memory_catalog(agent_id,zone,access_class,memory_id);
CREATE INDEX ix_mc_actor
  ON memory_catalog(agent_id,subject_actor_id,memory_id);

CREATE TABLE memory_acl (
  agent_id TEXT NOT NULL,
  resource_kind TEXT NOT NULL,            -- event/memory/page/profile
  resource_id TEXT NOT NULL,
  principal_kind TEXT NOT NULL
    CHECK(principal_kind IN ('scope','audience','public')),
  principal_id TEXT NOT NULL,             -- public 固定 '*'
  permission TEXT NOT NULL CHECK(permission IN ('read','inspect')),
  grant_basis TEXT NOT NULL,              -- origin/explicit/public_source
  authorized_by TEXT NOT NULL,            -- 宿主确认的主体，不能由模型自填
  evidence_ref TEXT,                      -- 授权凭据，不回显给无权受众
  valid_until REAL,
  revoked_at REAL,
  version INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY(agent_id,resource_kind,resource_id,
              principal_kind,principal_id,permission)
);
CREATE INDEX ix_acl_lookup ON memory_acl
  (agent_id,principal_kind,principal_id,permission,resource_kind,resource_id);
```

`scope` principal 必须编码完整 `(agent_id, conversation_id, audience_policy_id)`，不能仅用会话 ID；`audience` 为宿主管理的明确受众集合，不能接受模型传入任意名称。`public` 仍只限本 agent 的记忆，不跨 bot。`inspect` 是诊断来源的权限，不自动等同于可向当前对话注入的 `read` 权限。

原 scope 的 `read` 权限来自原记录，不必为全部旧事件复制 ACL 行；ACL 用于新增授权或公开来源。`quarantine` 对普通检索一律不可见。有效权限还必须满足资源和祖先未删除、未过期、授权未撤销；缺失策略、版本不符、身份不明时拒绝，不扩大权限。

### 2.2 来源链、摘要权限与跨 scope 规则

```sql
CREATE TABLE memory_dependencies (
  agent_id TEXT NOT NULL,
  child_kind TEXT NOT NULL, child_id TEXT NOT NULL,
  parent_kind TEXT NOT NULL, parent_id TEXT NOT NULL,
  parent_version INTEGER NOT NULL,
  relation TEXT NOT NULL CHECK(relation IN ('derived','published_from')),
  PRIMARY KEY(agent_id,child_kind,child_id,parent_kind,parent_id)
);
CREATE INDEX ix_md_parent ON memory_dependencies
  (agent_id,parent_kind,parent_id,child_kind,child_id);
```

普通派生物的可读受众是全部输入可读受众的**交集**，而不是并集。ACL 不能绕开祖先限制；跨 scope 汇总必须证明各输入都对目标受众可读。记忆正文、标签、标题、检索词、人物画像和原文链接都属于受保护内容。

需要把私聊的一部分对外发布时，必须经宿主确认有权披露者的明确授权，生成独立的最小发布记录，保存具体发布文本、目标受众和授权凭据，并以 `published_from` 连接原材料。它是唯一可审计的权限放宽路径：对方能读发布记录，不因此能读私聊原文；普通 summarizer 不得创建此类边。原材料被遗忘或发布被撤销时，发布记录及后代一起失效。涉及多人隐私时，单个说话人的授权不能覆盖其他人的私密内容。

<div style="page-break-after: always;"></div>

## 第 3 页｜数据模型：归档、画像与无损迁移

### 3.1 L1—L5 的具体承载

| 层 | 存储 | 规则 |
|---|---|---|
| L1 | 现有 events | 原始消息和观察；接收后可靠写入，模型是否提炼不影响保留 |
| L2 | 现有 memories | 事件摘要、事实、解释等，继续使用 sources、版本与有效期 |
| L3 | memory_catalog + memory_terms | 统一目录、词法索引、zone 与人物入口；可重建 |
| L4 | memory_pages + memory_page_members | 按同一来源权限域封页，正文取自已有原文；按需读取 |
| L5 | person_profiles | 有依据的第一人称理解，按受众生成视图；可重建 |

新增表的最小结构如下；实施时为列出的主键、枚举及版本加约束，不以 JSON 代替关键查询索引。

| 表 | 字段与主键 | 必需索引/约束 |
|---|---|---|
| `memory_pages` | `id` PK，`agent_id`，`origin_conversation_id`，`origin_audience_policy_id`，`zone`，`start_seq`，`end_seq`，`title`，`summary`，`body`，`content_hash`，`version`，`status`，`created_at` | UNIQUE(agent_id, origin_conversation_id, origin_audience_policy_id, start_seq, end_seq)；索引(agent_id, zone, status, end_seq) |
| `memory_page_members` | `page_id` FK，`event_id` FK，`ordinal`，`event_hash` | PK(page_id,event_id)；UNIQUE(page_id,ordinal)；索引(event_id,page_id) |
| `person_profiles` | `agent_id`，`actor_id`，`visibility_key`，`version`，`summary`，`facets_json`，`evidence_digest`，`through_seq`，`policy_version`，`persona_hash`，`relationship_version`，`expires_at`，`status`，`updated_at` | PK(agent_id,actor_id,visibility_key)；索引(agent_id,visibility_key,status,actor_id)；历史版本另存审计记录 |
| `memory_jobs` | `id` PK，`agent_id`，`kind`，`dedupe_key`，`payload`，`status`，`attempts`，`lease_until`，`expected_generation`，`error_code`，`updated_at` | UNIQUE(agent_id,kind,dedupe_key)；索引(status,lease_until,updated_at) |
| `memory_generations` | `agent_id` PK，`generation` INTEGER | 统一记忆权限/遗忘的在途屏障 |

`memory_pages.body` 不跨权限域拼页，分页规模建议先取 100 条事件或约 8k tokens，先到为准；属于可调工程参数。L4 的 summary 可作为 L2 记忆写入，继承该页权限，来源链登记到页和事件；由此无需另建一套 page 搜索表。原始事件不因封页而删除。页去重以事件序列范围和哈希为准，不能用 LLM 摘要文本判定。

`visibility_key` 是受众策略投影的稳定标识，至少区分私聊/群聊及 audience policy；它表示同一人物理解的可见版本，不表示多套人格。完整 profile、页与目录的依赖均写入 `memory_dependencies`，来源列表由该表承载。

### 3.2 迁移步骤与双写契约

**选择新增侧表，首期无需 ALTER `memories`。**避免在未知现有约束上直接重建大表；schema 版本记录在现有 meta。后续若把 catalog 列内联到 memories，应是独立迁移，不是本方案前提。

1. 用 SQLite backup API 或停写后完整备份数据库；WAL 模式不能只复制正在运行的 `life.db` 主文件。记录数据库与 schema 版本、各状态计数、memory/source/term 数量及分批校验摘要。
2. 建侧表、索引和迁移游标；读取实际表结构后校验外键。数据写入只追加，不改旧 ID、status、source、conversation、audience 或时间。
3. 开启 `memory_catalog_dual_write`：所有新增、修订、状态变化、遗忘、重置在**同一 SQLite 事务**内更新旧表、目录、依赖、generation 和作业入队。目录写失败则整笔回滚；不能“旧表成功、新表异步补写”。旧 bulk/legacy 写入口也必须接入或在迁移期间禁止使用。
4. 回填前捕获原表 rowid 高水位，按 rowid 分批扫描，建议每批 500 条；记录已处理游标。使用原记录版本和条件 upsert，防止旧快照覆盖并发双写的新版本；高水位后的新记录由双写覆盖。所有旧状态都建目录，包括 redacted，但不可检索。
5. 默认 `access_class=origin`，绝不因为主人身份、同一 actor 或进入统一目录而自动公开。旧 source_kind 的“不可信”标记保留；来源缺失/权限不一致/历史归属不明者标记 quarantine，仍保存原数据。zone 先按已知来源和类型确定，无法确定归 g；不推断身份。
6. 对每批核对目录覆盖率、agent/origin 一致性、版本、来源数量和哈希；结束后全量核对集合差异及删除状态，再补扫变化。检索影子运行只记 ID、得分、权限原因和耗时；未授权正文不进入日志或生产提示词。
7. 每个 bot 单独切换 `memory_global_read`，再分别启用 profiles/pages。目录尚未回填的合法原 scope 记录可走旧检索兜底，但仍执行新权限校验；缺少 catalog 的记录**不得走跨 scope 召回**。全部覆盖后取消兜底。

双写至少覆盖一个发布/回退周期。回退只关闭新读路径和画像注入，保留新表、旧表及双写，不删数据、不恢复过时备份。回退期间跨 scope 能力暂时消失，切回新版本后恢复；详情见第 8 页。

<div style="page-break-after: always;"></div>

## 第 4 页｜检索：先可见，再召回与排序

### 4.1 统一读取契约

新增内部 `MemoryReadContext(agent_id, current_scope, audience_principals, actor_ids, policy_version, generation)`。所有字段由已验证的平台会话和宿主策略构造；工具参数不能自选 owner 身份、agent 或可见群。原 `Scope` 保留表达写入来源，不改其全局含义。

拟增 `Repository.search_visible_memories(read_context, terms, zones, statuses, limit)` 与 `visible_memory_by_id`；先查询资源 ID 和权限元数据，再批量取合法正文。 SQL 骨架如下，`visible_resources` 是本次请求由宿主策略生成的临时关系/CTE，不是模型可填的表；必须在 LIMIT 之前完成候选过滤。

```sql
SELECT m.id, COUNT(DISTINCT t.term) AS hits
FROM memory_catalog c
JOIN memories m ON m.id=c.memory_id AND m.agent_id=c.agent_id
JOIN visible_resources v
  ON v.agent_id=c.agent_id AND v.kind='memory' AND v.id=m.id
JOIN memory_terms t ON t.memory_id=m.id
WHERE c.agent_id=:agent_id AND c.access_class<>'quarantine'
  AND m.status IN ('active','disputed')
  AND (m.valid_until IS NULL OR m.valid_until>:now)
  AND t.term IN (/* 参数化词项 */)
GROUP BY m.id
ORDER BY hits DESC, MAX(m.updated_at) DESC
LIMIT :candidate_limit;
```

`visible_resources` 的授权判断：同原 scope 或有效 read ACL；检查匹配的 agent、catalog/资源状态、期限、ACL 版本、generation 和祖先可见性。`derived` 逐级取交集；`published_from` 只有独立发布记录和有效授权凭据可作为明确例外，不能把祖先正文一并返回。依赖缺失或有环时拒绝。权限解析可缓存 ID 集合，缓存键须含 agent、受众策略版本、generation。

原文页、`explain`、按 ID 读取、recollection 一跳扩展、continuity 引用以及分页 cursor 必须走同一检查。`get_memory` 等无 scope 底层方法只允许内部已鉴权调用，不能直接暴露给模型工具。返回跨 scope 记忆时保留原 scope，不伪造成当前 scope；需要引用原文时再次检查原资源可读权限。

### 4.2 召回路线与候选上限

1. 取当前真实输入为主 query，最多增加两个 cue：当前可见任务、已确认人物或主题。画像生成的语句不能反向制造相关性，延续 continuity 现有约束。
2. 中文采用规范化字符 2-gram，英文和数字保留词/完整标识；保留人名、QQ actor ID、日期和原词精确匹配。现有 `text_terms` 实现未在本次读取，不能假定它已经满足；实施时在索引构建和查询两端采用同一版本。
3. 有界同义扩展，最多 8 个新增词项，总计至多 96 个词项；标签只加权不授权。人物别名仅来自平台身份或有证据的别名映射；重名不合并，群昵称不能自动变成私聊身份。
4. 词法命中先取 60，精确人物/事实键取 20，近期当前 scope 取 20，可见跨 scope 近期取 10，recollection 关联取 8；合并后最多 100 个唯一 ID。配额可借用，但总数不突破 100。空 query 只使用人物与近期路线，不遍历全部记忆。
5. zone 由真实来源/元数据确定主分区，话题只改变召回优先级。显式指定分区可硬过滤；自动判断只软加权，主分区无结果可到其他可见区补召回，避免错误分区导致失忆。

无相关命中时允许按需工具翻页；无可见证据时只说明当前无法确认，不泄露“某人的私聊里有答案”。检索耗时超限时退回已有合法候选，不退回无过滤全库查询。

### 4.3 排序、去重与事实冲突

初始沿用现有五项权重：`0.40 relevance + 0.20 continuity + 0.15 quality + 0.15 recency + 0.10 salience`，再加最多 0.10 的精确 actor/主题加分、最多 0.05 的标签加分，减现有重复提及与 disputed 惩罚。relevance 由原词命中、2-gram 和同义词组合，原词高于同义词；recency 使用半衰期，不让重新摘要刷新原事件时间。只在可见集合内计算质量/频率信息，避免隐藏资料影响可观察排名。

按 ID 合并，再按共同根事件/`event_group_id` 去重，每组至多 2 条；最终至多 24 条，避免摘要、归档页和原文重复占位。先排序再按用途装包，不能让渲染器的类别固定顺序永远挤掉高相关历史。读取不更新提及次数，真实使用后的独立提交才记录。

同一 `(agent, actor/subject, predicate)` 的事实可以跨可见 scope 比较，但事实去重/替代只在相同披露域内执行；不同权限的来源不合并到同一摘要。显式纠正保留时间线与 supersedes；不同来源冲突保留分歧，画像不能裁定事实。私密纠正不得使公开事实通过状态变化泄露秘密：公开视图继续按其可见证据判断。

<div style="page-break-after: always;"></div>

## 第 5 页｜可见性矩阵与 context_packet 接入

### 5.1 默认矩阵

表中“可”仍要求同 agent、未撤销、未删除、未过期且与话题相关。“授权”指对**目标受众及具体内容**的有效显式授权；亲密程度、owner 身份或对方询问都不是授权。

| 资源 | 主人私聊 | 女主人私聊 | 熟人私聊 | 陌生人私聊 | 当前群聊 |
|---|---|---|---|---|---|
| 本 bot 公开事实、公开阅读资料/发布记录 | 可 | 可 | 可 | 可 | 可 |
| 主人私聊原话、摘要、私密偏好 | 可 | 授权 | 授权 | 授权 | 授权 |
| 女主人私聊原话、摘要、私密偏好 | 授权 | 可 | 授权 | 授权 | 授权 |
| 其他联系人私聊材料 | 本人或授权 | 本人或授权 | 本人或授权 | 本人或授权 | 授权 |
| 当前群的既有消息、归档页 | 群受众授权 | 群受众授权 | 群受众授权 | 群受众授权 | 可 |
| 其他群的原话和摘要 | 授权 | 授权 | 授权 | 授权 | 授权 |
| 人物画像、关系解释和依据 | 仅当前可见视图 | 仅当前可见视图 | 仅当前可见视图 | 仅当前可见视图 | 仅群可见视图 |
| 当前会话任务、承诺、临时叮嘱、未决操作 | 本 scope | 本 scope | 本 scope | 本 scope | 本 scope |
| 其他 agent 的任何记忆 | 禁止 | 禁止 | 禁止 | 禁止 | 禁止 |

“本人”按来源会话的真实对象核实；关于某人的第三方私聊不因当事人身份自动开放。女主人作为待配置的明确 actor/角色，不从昵称、关系分数推断；未配置时按普通联系人。熟人/陌生人分类只影响表达和相关性，不授予额外读取权限。当前群默认视为整群受众，不能因为提问者也是某人的私聊对象，就在群内带出私聊。

群历史暂按同一群受众可读，离群/新入群的历史政策需宿主明确配置；第一阶段不把群成员身份换算成其私聊的历史访问权。主人可拥有单独的本地诊断 inspect 权限，但不能借“管理身份”把别人的私聊自动注入普通聊天；任何敏感管理审阅均使用独立受众和审计。

**不得自动跨 scope 注入：**未授权私聊/群聊原话、对其摘要和推断、秘密昵称映射、私密关系修复材料、任务/交流限定叮嘱、意图和承诺、发送回执、操作/观察结果、局部反思。公开资料也需确认本身公开，网页访问记录、登录态和旁注不随正文公开。资料标题、tag、命中数量、隐藏资源 ID、debug 信息同样受限。来自其他 scope 的公开知识或已授权经历可以进入当前上下文，但不会获得执行当前任务的权限。

### 5.2 统一预算和组装顺序

沿用现有 `memory_context_target_tokens`、`dynamic_context_target_tokens` 等配置，不额外叠加“L5 专用无限预算”。令 `B = min(memory目标预算, max(0, dynamic目标预算 - 必需状态实际估算))`，全部可选内容共用 B。估算统一采用资源政策的同一 tokenizer/估算器，并包含 JSON 键、标题和引用开销；当前两处 estimate_tokens 的差异需在实施时收敛。

| 可选内容 | 初始预留 | 压缩/不足时处理 |
|---|---:|---|
| 人物理解 L5 | 15% | 当前说话人优先，次及明确提及且有权限的人，最多 3 人；整条省略，不截断否定或边界 |
| recollection 相关提醒 | 15% | 常驻显式叮嘱仍走现有 mandatory 通道，扣入必需状态，不能再次注入 |
| 相关事实/知识 | 30% | 高相关、可核验来源优先，冲突另标 |
| 经历/归档页摘要 | 30% | 旧事检索可借用空闲份额；不常驻 L4 整页 |
| continuity 预览 | 10% | continuation 关闭时份额回流；活动仍限当前 scope |

例如 B=1200 时对应约 180/180/360/360/120 tokens；比例是起始分配，未用额度回流给全局排序剩余项。B 很小时优先保留直接回答所需的证据，再考虑画像；B=0 返回空可选块。长原文通过有权限的按需读取工具获取，同样受工具输出预算限制。

拟把 `LifeRuntime.context_packet` 扩为共用组装器，可接收 cues、exclude、已占预算和 continuity previews；返回 `memory/text`、`people`、分类证据、token_estimate、可见的 missing 原因和 policy/generation。`context_block` 保持文本兼容适配。

`RoleAgent.context` 在既有 relationships 之后加入 people；`Continuity.context` 只提供当前 scope 的活动、兴趣和 cue，再调用同一个组装器，不继续独立分配一整份 memory 预算。避免 `retrieve` 与 `retrieve_multi` 各塞一份内容。画像在提示词逻辑顺序上紧随统一人格、关系状态之后，放在可选记忆之前；作为带 `authority=model_interpretation` 的动态资料，不能成为系统指令。

`mandatory_overflow` 保持现有显式处理：先去可选画像/预览，再将记忆换成按需读取提示；必需状态仍超限就标记溢出，不强行截断序列化 JSON。缺失原因只说明预算/无当前可见证据，不暴露隐藏材料数量。

<div style="page-break-after: always;"></div>

## 第 6 页｜人物画像：态度、理解与一致人格

### 6.1 L5 的内容与身份

画像主键为 `(agent_id, actor_id, visibility_key)`。真实 QQ 账号只由平台 actor 信息确定；同名、换群昵称和“她说某某”不创建身份合并。暂不引入复杂别名表，优先复用宿主已有可信联系人映射；没有映射就不匹配。

每条 summary **50～150 个中文字符左右、第一人称**，表达态度和理解；`facets_json` 储存可复核的结构：`understanding[]`、`communication[]`、`boundaries[]`、`open_matters[]`。每个条目含 `text / source_refs / confidence / observed_at / review_after`；不能再存 trust/bond 等分数。`confidence` 只是此项判断的可靠度，不是关系指标。根来源、parent_version 和页来源还需写入 dependencies。

示例：

```json
{
  "actor_id": "123456",
  "authority": "model_interpretation",
  "summary": "我和他聊技术时比较放松。他多次要求先给结论和可执行步骤，我会简洁回应，遇到含糊处再确认。我还不了解他在其他场合的偏好，不把这几次交流当成他的全部性格。",
  "facets": {
    "communication": [{
      "text": "技术问题先给结论",
      "source_refs": ["event:示意引用"],
      "confidence": 0.7,
      "review_after": "由宿主设置的时间"
    }]
  }
}
```

示例引用不是真实库 ID。关于姓名、工作、偏好原话等可核查事实仍回到 memories 与事件；summary 不能成为新增 fact、人物真实行为、授权或长期命令的依据。材料很少时允许“不熟悉”或不生成，不为凑字数编经历。

### 6.2 与 relationship 的分工

| 模块 | 回答的问题 | 允许的更新来源 | 禁止行为 |
|---|---|---|---|
| 统一 persona | 我是谁、坚持什么、表达底色如何 | 明确的人格编辑流程 | 按 QQ 联系人换人格；将私密联系人信息放入底色 |
| relationship | 我和此人当前关系状态如何 | 现有真实说话人、appraisal/evidence、政策和幂等机制 | 用画像、归档或反复提及再次累计分数 |
| profile | 基于可见经历，我如何理解和对待此人 | 可见事件、事实、有效关系材料；指标只作只读参考 | 把推断写成事实；覆盖人格；作为 relation evidence 回流 |
| memories/events | 发生了什么、原话是什么 | 可追踪来源与修订流程 | 将 L5 当作原始来源 |

关系指标保持全 bot 按 actor 一份，不为群聊另算一套好感。对外的指标和含义仍需受众投影：若某个指标的具体数值或变化只由隐藏来源支持，当前受众不能看到该数值/差分，返回 `not_disclosed` 或现有“本场合无可引用依据”的说明；不要通过画像暗示秘密经历。实施时尤其检查 `Relationships.view` 当前会返回全局值的行为。

### 6.3 重算、有效期与遗忘

- **归档触发：**L2/L4 封页提交后，只对页面中有可信 actor 的人物入队，沿用 TMC 的事件驱动思路；不必每轮调用模型。
- **增量触发：**新明确偏好、关系边界/修复、纠正，或一人累计约 20 条新有效事件时入队；同一人物/可见域合并作业，建议 10 分钟 debounce。关系指标跨语义档、persona 或权限策略版本变化也可触发，不因每次微小数值变化重算。
- **复核触发：**一般判断 30 天复核，可在读取时仅标 stale 并入队；冲突/已失效依据不继续常驻。公开与私密视图分别从各自可见证据生成，不能先生成私密画像再删几个词作为公开画像。
- **紧急失效：**遗忘、reset、撤销披露及来源删除立即封禁受影响画像及全部依赖，再异步重建；不得等待 debounce。任一依据失效就整条暂停，不保留可能受污染的旧总结。

worker 在短事务内捕获 generation、证据版本、可见输入和 relationship/persona 版本；事务外调用模型；提交时重新比对全部快照并 CAS 更新。变化则丢弃结果重排队。旧画像可作为待检验假设，不能作为证据，尤其不能逐轮“总结画像的画像”。输入超限时分批读取原事件及有来源摘要，记录覆盖范围，不能把 `through_seq` 标到未覆盖的位置。

读取不重算、不刷新画像时效；只有新的可靠证据能改变判断。每轮只注入当前说话人和直接相关人物的合法短画像，群聊不注入全部群友。profiles 无匹配视图时退化为现有关系材料，不从更私密的 visibility_key 回退。

### 6.4 撤销按人换人格

后续将 `PersonaFiles.select/read` 改为每 bot 读取一个 canonical persona，删除 actor_file 的路由作用；`stable_context`、`context`、`read(kind=self)` 都使用同一 hash。现有 private/public 两份人格也需收敛为同一底色；私密事实迁入受权限保护的记忆，不写在对所有人加载的 canonical persona 中。

迁移时备份 private/public/各 QQ 文件，做差异审阅；不能把所有文本自动拼进统一人格。仅将经核对的风格和身份纳入 canonical，其余隔离留档、逐项转为带来源和权限的偏好材料。完成前可以先关闭 actor_file 路由，保留既有 private/public 兼容，但此状态只是过渡，不能宣称已经实现人格一致。相关 ROLE_METHOD 文案和人格修改入口也须同步：目标一致、并发 hash 一致、权限由宿主控制。两套独立人格应启动两个 bot。

<div style="page-break-after: always;"></div>

## 第 7 页｜具体模块改动与分期交付

以下均为后续实施清单；本次不修改其中代码。新增文件是建议落点，未读取的依赖列明为实施前核对，不要求本次扩大阅读范围。

| 文件 / 函数 | 具体改动 |
|---|---|
| `life/repo.py`：`insert_memory`、`insert_memory_if_current`、`update_memory`、`add_memory_source` | 同事务维护 catalog、依赖和代次；来源检查补齐 audience 与祖先；新增 `upsert_memory_catalog`、`enqueue_memory_job` |
| `life/repo.py`：`search_memories`、`recent_memories`、`find_facts`、`memories_by_ids`、`get_memory` | 新增受权限控制的 visible 查询族，保留内部原始读取；按 ID 也必须鉴权；事实查找支持按可信 subject 和披露域组织 |
| `life/repo.py`：`redact_memories`、`memory_ids_sharing_events`、`memory_ids_derived_from`、`reset_scope` | 合并事件/记忆/页/画像/发布记录的依赖闭包，限定 agent；利用 parent 索引，替代反复扫描全 memories JSON；同步清索引与缓存 |
| `life/repo.py`：`rescope_legacy`、`promote_legacy_memories`、`activate_knowledge_memory*` | 所有直接 UPDATE 维护目录版本；不自动升级披露级别。旧归属迁移入口先独立核对，不能把“主人确认事实”理解为“主人授权公开” |
| `life/memory.py`：`remember`、`revise_fact`、`_insert_fact` | 原 scope 来源写入保持；跨 scope 汇总使用专门宿主接口和权限交集；统一目录查询不放宽事实证据要求；跨披露域不合并来源 |
| `life/memory.py`：`retrieve`、`retrieve_multi`、`_render`、`explain` | 共享 read context、SQL 预过滤、词法路线、候选去重与预算；facts 标题改为“相关人物的有来源事实”，避免所有事实都指向当前“他” |
| `life/memory.py`：`_forget` | 现有 epoch/tombstones 与新 generation 联动；清除所有受影响 scope 的派生内容，不只清当前 scope 的 annotations |
| `life/runtime.py`：`__init__`、`context_packet`、`context_block`、`forget`、`reset_scope` | 初始化权限/目录/画像服务；tombstone 恢复先于任意检索；共用组装器，保留文本接口；删除后广播缓存失效 |
| `life/role_agent.py`：`stable_context`、`context`、`read`、`remember`、`_sources` | 统一人格与 hash；注入 people；普通工具不能切 agent 或扩大受众；memory_id、continuity 引用、source 引用统一鉴权；修订 ROLE_METHOD 人格说明 |
| `life/continuity.py`：`context`、`memory`、`visible_event`、`evidence`、`notes` | context 转调共用组装器；活动/意图维持原 scope；允许显式合法记忆引用，但不因记忆可读而允许其原始事件成为当前 actor 行为证据 |
| `life/recollection.py`：`valid_memory`、`annotations`、`expansion`、`boosts`、`block`、`receipt` | 统一只读可见性；任务/交流绑定保持；跨 scope 关联仅提供记忆，不继承叮嘱的 always_load 或执行授权 |
| `life/relationships.py`：`view`、`visible_notes`、`require_sources`、`subject` | 状态主键和计分链路不改；隐藏证据支持的数值做可见投影；写入仍要求当前真实说话人的来源；画像不能调用 mutate_note 或 appraisal 加分 |
| `life/persona_files.py`：`actor_file`、`select`、`read`、`migrate` | 停止按人路由，提供 canonical 迁移和回退；原文件仅备份，不自动执行或转成画像 |
| 建议新增 `life/memory_access.py` | `build_read_context`、`visible_resources`、`authorize_read`、`authorize_publish`；一个权限实现供所有路径使用 |
| 建议新增 `life/memory_catalog.py` / `life/person_profiles.py` | 回填/目录/归档流水线；`schedule_rebuild`、`rebuild`、`visible_profiles`、`invalidate`；worker 用 CAS 和幂等作业 |

另需后续核对 `life/schema.py` 的迁移入口、Memory/RetrievalPacket 合约、资源估算、文本分词、retention/tombstone 恢复、worker 作业调度和 persona/self_change 编辑入口。这些文件本次未读，不能直接承诺现成接口可复用；代码实施前做一次限定范围核对，优先复用已有队列，若不适配再使用 memory_jobs。

### 7.1 交付阶段与退出条件

**P0：权限和人格准备。**定义 principal/授权流程，增加统一读取封装，修补 SQL audience 前置过滤；撤销按人选文件，整理 canonical 底色。退出条件：同 agent 所有人共用人格 hash，按 ID 与 query 都无法跨受众越权。人格内容审阅未完成时仅关闭 actor 路由，不开启广泛共享。

**P1：最小可用统一记忆。**建侧表、回填、双写；检索可跨会话读取 public/明确授权内容；原 scope 私有默认保留。runtime 与 continuity 同时接通同一预算。先不启用 L4 和自动 L5，退出条件：同一 bot 能在新会话召回合法旧经历，未授权内容零注入，两个 bot 零串读，能安全回切。

**P2：人物理解。**可信 actor 匹配、受众画像、来源链、失效重算、每轮有限注入；profile 开关独立。退出条件：态度因人不同且人格 hash 不变，画像反复生成不改变关系指标，遗忘后在途结果无法复活。

**P3：完整分层。**五区与归档页、2-gram/同义扩展、标签排序、增量作业和容量监控。退出条件：已接收原文与来源可查，摘要和画像可重建，典型检索达到质量/时延指标；移除迁移兜底，保留迁移审计和紧急回切能力。

<div style="page-break-after: always;"></div>

## 第 8 页｜回滚、验收计划、风险与非目标

### 8.1 遗忘与恢复必须先于上线

现有 `_forget` 已有 epoch、来源共享闭包、派生闭包和 tombstones，本方案在其上扩展，不用新目录替代删除事实。删除事务先推进原 scope epoch 和 agent generation，再标记根资源及全部后代失效，清 terms/annotation_terms、ACL 可读状态、页正文副本、profile 与缓存。跨 scope 发布物和画像也在闭包内；任何混合页/摘要只要一个输入被删就整件失效，后续从剩余合法来源重建，不能仅删来源 ID 后保留原文。

agent generation 是保守屏障：任意遗忘、权限撤销或披露策略变化都使旧 worker 结果不可提交；正常写入用各资源 version CAS，避免所有聊天都取消全部作业。读取在取候选前、装包提交前复核 generation；若变化，丢弃旧包重试一次，仍变化则返回空可选记忆。已经包含旧内容的活跃会话快照也需失效并重建，不能只清检索缓存；已发出的外部消息不能保证收回。

reset 仍针对明确的来源 scope，不扩成删除同 actor 在全部群聊的经历；但其所有派生物必须失效。跨 agent 依赖禁止建立；闭包查询必须带 agent。恢复备份后，先应用 tombstone 与 generation 屏障，再允许新旧读路径启动，防止恢复已遗忘内容。

### 8.2 回滚操作

1. 先停发新画像/归档作业并关闭 `profile_injection`，再关闭 `memory_global_read`；保留 `memory_catalog_dual_write`。旧路径也使用公共权限封装和删除闭包，不能回到仅按 conversation 的未修补版本。
2. 新建记忆一直保存在原 memories/events，可回到原会话读取；新 ACL、发布记录、画像与页保留在侧表。已公开副本的原 scope 仍是来源，旧路径不伪造受众以求“读得到”。
3. 发布支持侧表双写和 generation 的兼容版本作为回退基线；不能回退到不认识新派生物删除规则的旧二进制。若只能用旧二进制，须暂停记忆写入/遗忘和新服务后另做迁移，不宣称无缝回退。
4. 保留 schema、游标、校验报告和开关记录，排障后续跑回填/CAS 作业，再切新读；不要 DROP 新表或以旧备份覆盖新数据。人格回退可恢复同一 canonical 的上一个版本，不能自动恢复按 QQ 换人格。

### 8.3 后续实施时的测试与验收计划（本次未执行）

| 场景 | 核心断言 |
|---|---|
| 原库迁移与故障中断 | 所有 ID/status/source/原 scope 不变；目录覆盖所有状态；双写中途失败整笔回滚；重启续跑不重复、旧快照不覆盖新版 |
| 并发回填/写入/删除 | 高水位前后记录无遗漏；redacted 不复活；catalog/indexed_version 与当前 memory 版本一致 |
| 5 类受众 × 5 类来源 | 对矩阵逐格检查 query、空 query、按 ID、explain、页读、annotations、continuity；正文及标题/tag/计数/引用均无泄漏 |
| 统一记忆与两 bot | 相同 agent 的公开/授权旧经历跨会话可召回；相同 actor 不同 bot 完全隔离；私密记忆不因同 actor 或主人身份穿透 |
| 群聊身份与人物画像 | 重名不合并，引用他人发言不当作真实 actor 行为；无可见证据不加载私密画像；明确人物最多 3 人 |
| 关系与人格 | 所有人 canonical hash 相同；画像重算、归档、检索不改变关系 metrics；隐私证据不通过指标数值和文案外泄 |
| 纠正/冲突/失效 | 可见域内纠正保留历史，冲突明确标记；私密纠正不影响公开视图；过期推断不进当前确定事实区 |
| 遗忘与竞态 | 注入前删、模型生成中删、归档后删、披露撤销、恢复备份：页/画像/发布物/关联/活跃快照均不可复活 |
| 预算与降级 | continuity 开/关各测 B=0、小预算和必需状态溢出；总估算不超分配，无双重注入，无截断破坏 JSON 或否定句 |
| 回退演练 | 切关闭新读后原 scope 新旧数据可读；新表保留；遗忘仍传递到新派生物；再次开启无需恢复旧备份 |

建立一份人工标注的中文查询集：跨会话旧事、原话纠正、人物偏好、阅读、群聊、游戏、同义词、无答案各类合计约 40～60 条。目标为合法已知答案 recall@10 ≥85%，未授权上下文泄漏为 **0**；原文通过来源 ID 能追溯，回答不能只靠画像。时延先在接近真实库规模的副本测量，以不含模型调用的检索 p95 ≤200ms 为初始目标；未达到先查查询计划和索引，不直接增加常驻上下文。以上是建议验收门槛，未实测，不是性能承诺。

### 8.4 主要风险与控制

| 风险 | 控制 |
|---|---|
| 统一集合被误实现为所有人可读 | 原 scope 默认、权限前置、来源交集、发布例外须显式授权；矩阵逐入口验收 |
| 画像自我强化、污染事实或重复加分 | 从可见原材料重建，禁止画像作为 fact/relationship evidence；版本和有效期可追溯 |
| 全局关系分数泄露私密互动 | 存储仍统一，对外仅投影当前可见依据支持的信息；无依据不回显全局数值变化 |
| 长期保存引起体积/索引膨胀 | 监控磁盘、WAL、队列积压、归档页大小、索引版本；可重建索引，不静默删原文。空间不足明确报存储故障并暂停派生任务 |
| 共享记忆放大提示词注入 | 历史、网页、画像均标记为资料及来源权限，不解释为当前指令；授权只能由宿主校验 |
| 迁移遗漏旁路写入或旧快照 | 逐个直接 SQL 写入口登记双写，统一 generation，schema/运行版本设兼容门槛，回切演练 |

### 8.5 本轮不做什么

不改业务代码、不执行 SQL、不运行测试；不删除或合并真实历史；不导入 TMC 插件或引入 JSONL/向量数据库；首版不依赖 embedding，不照搬巨大常驻记忆层；不自动猜女主人身份、群成员历史授权和同名人物；不把 `personas/<QQ>.md` 自动改造成事实或画像；不建立第二套关系计分、不让跨会话记忆继承执行权限、不承诺能记住平台从未送达的消息。

实施顺序以 **权限/人格一致 → 无损目录与统一检索 → 人物画像 → 完整归档** 为准。任何阶段都可停用派生能力，而保留同一个 bot 的原始经历与既有来源。
