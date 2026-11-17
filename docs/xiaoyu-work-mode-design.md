# 小鱼的工具模式与上下文设计（评估稿）

> 目的：在不污染她日常对话上下文的前提下，让她具备接近 Codex / DSH 的工作能力，并支持子 agent。
> 本文只做评估与设计，不含实施。所有数字来自服务器实测。

---

## 1. 先纠正一个直觉：她不是上下文爆炸，是上下文挨饿

她有一套**很严的**上下文预算（`life/companion_life_material.py`）：

| 槽位 | 预算(token) | 装什么 |
|---|---|---|
| history | 1600 | 最近对话 |
| projects | 650 | 目标与下一步 |
| today | 650 | 今天的活动 |
| recall | 600 | 旧材料召回 |
| notes | 550 | 人物/自我笔记 |
| ambient | 200 | 时间、连接、新事件 |
| recollection | 150 | 一条旧事 |
| **合计** | **4400** | |

代码注释明确写着：**"工具回执有意留在预算之外"**。也就是：

- **当前轮**：工具输出全文（最长 12000 字符）作为本轮输入进入，不受 4400 限制 → 这一下确实很大；
- **下一轮**：它变成 history 候选，但 history 只有 1600 token（按 `estimate_tokens`，汉字≈1 token/字）；
- `fit()` 的规则是 `price > budget` 就**整条丢弃**。

**结论：一条 3000 字的命令输出，当轮她看得到，下一轮就彻底消失了。** 这解释了她为什么反复重查同一件事——不是她笨，是她真的记不住自己的工具回执。

> 附带发现：预算注释引用的 `docs/companion-next-agent-handoff.md §3` 在服务器上**不存在**；`computer` 提示词引用的 `skills/computer.md` 也**不存在**。设计依据和技能文档都没部署上来。

---

## 2. 真正的瓶颈是信噪比，不是 token 总量

一次 grep 命中 300 行，她要的是其中 3 行。把 300 行塞进上下文，既没让她更聪明，又挤掉了别的记忆。

所以设计目标不是"加更多工具"（那只会让轮次更多、输出更多），而是：

**让工具产出"可直接决策的摘要 + 可回溯的引用"，而不是原文。**

---

## 3. 目标工具面（对标 Codex 与 DSH）

Codex 在本机的实际配置（`~/.codex/config.toml`）：

- MCP 服务器：`node_repl`（浏览器/JS 执行）、`windows-mcp`
- 插件：`browser`、`computer-use`、`documents`、`pdf`、`spreadsheets`、`presentations`、`visualize`、`template-creator`

她的 venv 现状（实测）：

```
OK   mcp 1.30.0      ← MCP 客户端已就绪，不必依赖 DSH
OK   httpx 0.28.1  bs4 4.15.0  lxml 6.1.3  PIL 12.3.0  sqlite3
MISS docx  openpyxl  pptx  playwright
```

### 建议的工具分层

| 层 | 内容 | 现状 | 落地代价 |
|---|---|---|---|
| **L1 核心** | read / write / edit / grep / list / run | **已有**（本次新加的 `work`） | 0 |
| **L2 检索** | web 搜索/抓取、sqlite 查询 | 已有 `web` | 0 |
| **L3 扩展-MCP** | filesystem / sqlite / fetch / codegraph 等任意 MCP | 客户端已装，未接线 | 中（进程托管 + 超时） |
| **L4 扩展-技能** | `skills/*.md`，只把 name+description 放提示词，用到再 load | **目录不存在** | 小（自建 + 加载器） |
| **L5 文档** | docx / xlsx / pptx / pdf 读写 | 库未装 | 小（`pip install`，约 20MB） |
| **L6 编排** | 子 agent（delegate）、有限并发 | 仅 `kind='dsh'`，运行时缺 | 中（原生实现） |

不建议：playwright / 浏览器自动化。2 vCPU + 3.4GB 无 swap，一个 Chromium 就能把机器打满。

---

## 4. 四种上下文纪律（这是整套设计的核心）

### 4.1 回执做"三层截断"，而不是一刀切

```
完整输出 → artifacts/<id>.log          （永不进上下文）
         → 上下文只放：命中行 / 首尾各 N 行 / 摘要 + output_path + sha
```

她已经有 `artifacts` 和 `truncated` 机制，只是**没有把"引用"回传给她**。现在回的是 12000 字符原文，应该回的是"我跑了什么、退出码、命中 3 行、全文在哪个路径"。

### 4.2 分层预算（把"当前轮"也管起来）

| 层 | 预算 | 内容 |
|---|---|---|
| L0 常驻 | ~2500 | 人格、规则、工具说明（现约 4400 的固定部分） |
| L1 当前轮 | **≤2000** | 用户这句话 + 本轮工具回执摘要（硬上限） |
| L2 工作记忆 | 4400 | 现有 7 槽 |
| L3 档案 | 无限 | 落盘全文，靠 `read`/`grep` 按需取回 |

关键改动：**L1 也要有上限**。否则一次 12000 字符的回执就吃掉了整个 working memory。

### 4.3 子 agent 隔离（context firewall）

父 agent 只发：`goal + 允许的范围 + 验收标准 + 预算`。
子 agent 在自己的上下文里读 500 行文件、跑构建、看报错、重试。
父 agent 只收：`结论摘要(≤800字) + 改了哪些文件 + 验证命令的真实输出尾部 + 产物路径`。

这样"读代码/改代码/跑测试"的噪声全部留在子 agent 里，父的上下文纹丝不动。

### 4.4 渐进披露（skills）

提示词里只放：

```
可用技能：office-docx（读写 Word）、office-xlsx（表格）、pdf（抽取/生成）
```

模型决定要用时，再 `skill load office-docx` 把全文读进来。**几十个技能也只占几行 context。**

---

## 5. 子 agent 的两条实现路线

### 路线 A：原生 Python delegate（推荐先做）

她的 `DirectModel.complete()` 已是 OpenAI 兼容异步客户端，`api_key / base_url / model / max_tokens` 齐全。
用同一端点起一个迷你循环，工具就是 `work` 那 6 个，步数封顶。

- 代价：约 200 行，不碰 node
- 能力：能干"读文件→改→跑→再改"这类 90% 的活
- 局限：没有 skills/plugins/MCP，不能递归派活

### 路线 B：DSH 子 agent —— **已安装完成（2026-11-16）**

原以为要搬 25,663 个源码文件 + 重建 Linux 依赖，实际发现 **`@deepseek-ai/dsh` 已发布在 npm**（44 个版本），直接装隔离环境即可，完全不用源码树、不用 tsx、不用 pnpm。

最终落地的结构：

| 位置 | 内容 | 大小 |
|---|---|---|
| `/opt/qq-deepseek/toolchain/node` | 独立 Node 22.19.0（不碰系统 node 18） | 193M |
| `/opt/qq-deepseek/toolchain/dsh` | `@deepseek-ai/dsh@0.2.1-rc.2`（与 Python SDK 版本对齐） | 858M |
| `/opt/qq-deepseek/vendor/dsh-runtime` | `lib/bin.js` 软链 + Python SDK（6 个文件） | 156K |
| `/var/lib/companion/xiaoyu/data/dsh-linux` | 全新 `DSH_HOME`（旧的是 Windows 会话，格式不兼容） | — |

踩到并解决的两个坑：

1. **会话格式冲突**：旧 `data/dsh-home` 里的 `.jsonl.zstd` 会话与新运行时默认的 `compression=none` 不兼容，报 `use a separate root or select the matching compression mode`。换新 home 解决。
2. **`/anthropic` 路由**：`deepseek-official` provider 走 Anthropic Messages 协议，base 必须是 `https://api.deepseek.com/anthropic`；若被覆盖成根地址，Anthropic SDK 会拼成 `/v1/messages` 并返回 404（已实测复现）。原代码 `companion_executor.py:131` 本来就处理了这一点。

**实测结果**：
- 纯问答：3.06 秒，`completed`
- 带工具的活（"查看目录内容并原样报告"）：5.44 秒，她真实执行了 `find ... | head -6` 并返回真实目录内容

---

## 6. 实测账本（2026-11-16）

从数据库 `companion_context_loads` 取最近 25 次认知的 `request_tokens`：

```
中位 13,545   最大 15,981   最小 13,134      （天花板 16,000）
```

每轮固定开销拆解（与内容无关，每次都一样）：

| 组成 | tokens | 占每轮 |
|---|---|---|
| 人格 persona | 2,890 | 21.3% |
| METHOD（行为准则） | 2,256 | 16.7% |
| MOTIVATION_INSTRUCTIONS | 395 | 2.9% |
| ATTENTION_INSTRUCTIONS | 245 | 1.8% |
| 工具说明（5 类） | 646 | 4.8% |
| **固定小计** | **6,432** | **47.5%** |
| 生活材料上限 | 4,400 | 32.5% |
| 状态块 + 历史 + 当前输入 | ≈2,700 | 20% |

**结论：一轮 12 步的活 = 12 次唤醒 ≈ 77,000 tokens 的纯重复文本。**
这正是"重复把没用的上下文给到模型"的量化形态，也是 CPU 高的根源（每次唤醒还要重跑召回 SQL、重建 4400 token 材料）。

## 7. 分阶段建议

| 阶段 | 内容 | 影响 | 状态 |
|---|---|---|---|
| **A** | 装上 DSH 子 agent（隔离 Node + npm 包 + 运行时根 + 代码适配） | 她第一次能"派活" | **✅ 已完成并实测** |
| **B** | 工具说明改成"多步工作一次交出去" | 12 次唤醒 → 1 次 | **✅ 已部署** |
| **C** | 回执唤醒不再灌全套生活材料（town/bilibili/motivation 与本次动作无关时省略） | 再省约 2,700/轮 | 待做 |
| **D** | MCP 接入（filesystem / sqlite / codegraph） | 工具面陡增，几乎零上下文成本 | 待做 |
| **E** | `skills/` 目录 + 渐进披露 | 几十个技能只占几行 | 待做 |
| **F** | 装 office 库（docx/xlsx/pptx） | 文档能力 | 待做 |

### 顺序上的理由

阶段 0 必须先做：**工具越多，回执越大，不先修上下文纪律等于加速变傻。**
阶段 2 的性价比最高：MCP 是外挂进程，工具进上下文只有 schema（几百 token），真正干活的开销都在外部。

---

## 7. 必须提前定的三条红线

1. **配额**：子 agent 每次派活要计入预算。建议 `max_steps ≤ 12`、单任务 token 上限、每轮最多 1 个 delegate。
2. **写权限**：她现在能改 `/opt/qq-deepseek/life/` 下**自己的源码**（companion 属主）。要不要限制成只允许 `work/` 和指定目录？
3. **回滚**：她自己改代码前，是否强制留备份/或走 git 提交？现在没有任何保护。

---

## 8. 一句话总结

她缺的不是"更多工具"，是**工具结果的表达方式**。
把 12000 字符的原文换成一个路径 + 3 行命中 + 退出码，"上下文爆炸"这个问题就不存在了；
在此之上加子 agent 和 MCP，才是在加能力，而不是在加噪声。
