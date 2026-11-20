# P44 blocker 修复交接清单

日期：2026-11-18　执行：isolation-verifier　HEAD：`09d91c8`
**以下 md5 全部是部署后现取的**（`md5sum` 于本文写作时执行，非提交时快照）。
服务：`active` / `NRestarts=0` / `alive 200`。

---

## 1. 文件与当前 md5

| 文件 | md5（现取） | 本轮改动 |
|---|---|---|
| `life/companion_ask.py` | `7c4fd7db` | **B-1**：新增 `questions_view()`，产出 GUIDES 承诺的 `current_state.questions` |
| `life/companion.py` | `809538c0` | **B-1**：把上述挂进 `state['current_state']`（`self_process` 旁边的位置） |
| `life/companion_tasks.py` | `d567c3e2` | **B-4**：`clear()` 真写 `hidden_at`；`list()` 默认折叠、`status="done"` 仍可见 |
| `life/companion_remote.py` | `c9f010b7` | rpc 失败改为长期 `log.exception`（脱敏），补 `import logging` |
| `life/providers.py` | `fdcd2f94` | **B-7**：加 `tokenhub.tencentmaas.com` 前缀 + import 时自检 |
| `life/companion_awareness.py` | `113a9cd1` | **B-3**：`reason` 按 `status` 取词，三种 `needs_setup` 各给原因 |

以下文件本轮**没有改**，列出来是因为交接时需要对齐：
`companion_background.py ad767d64`、`companion_executor.py 6235165b`、
`companion_context.py 96b4810e`、`companion_work.py 053ccc85`（capability-engineer 的 t26）。

---

## 2. 逐条结论

| Blocker | 结论 | 一句话 |
|---|---|---|
| **B-1** ask 回看断链 | **真做了** | `current_state.questions` 现在真产出；她能答出「刚挂的是哪条、您答的是不拆」 |
| **B-4** task clear 撒谎 | **真做了** | 写 `hidden_at`；默认列表折叠，`status="done"` 仍能看全，库里**零删除** |
| **B-6** 注释教旧字段名 | **判据不成立，没改** | 全文件 `provider_tools` 出现 0 次；第 136 行写的就是真实字段 `model_supports_tools`，且真生效 |
| **B-7** 腾讯新地址认不出 | **真做了** | 新地址 → `tencent`；旧地址仍认；不认识的仍返回 `None`（不猜） |
| **B-3** 通道条目分裂 | **真做了** | reason 改按 status 取词；`disabled/unconfigured/login_required` 三种各自给出不同原因 |

**B-6 特别说明**：这条我判为「评审判据不成立」，理由是
`providers.py` 里 `provider_tools` 出现 0 次，注释写的是 `model_supports_tools`，
而后者是 `role_config.py:34` 的真实字段、被 `companion_model.py:81` 消费、`False` 真的落到 json_mode。
**我不想因为「评审说了」就去改一段本来正确的注释，那会让注释和代码真的产生分歧。**
队长已核实并同意。

---

## 3. 额外做的两件（不在 blocker 列表里）

1. **rpc 失败长期记堆栈**（队长要求）：`logging.getLogger(__name__).exception(...)`，脱敏（不记 headers/body/token），调用方仍只拿类型名，`im_*` 的 detail 透出行为不变。起因是一次**瞬时 `ValueError` 只报了类型名，根因没抓到**。
2. **providers import 自检**：任何 PROVIDERS 的 `base_url` 若不能被自己的 id 认出来，**import 直接失败**。把「新加端点忘了加 `_PREFIX`」从「静默退化成 json_mode」变成「不可能上线」。

---

## 4. 已知未验证（交接请接手）

| 项 | 为什么没验 |
|---|---|
| **腾讯 `supports_tools`** | 云端无 key，未实测 tool_calls。档案里 `supports_tools=True` 是假设；不支持时靠配置 `model_supports_tools=false` 兜底（这条兜底已实测有效） |
| **腾讯 `list_models_supported=False`** | 依据是 `GET /plan/v3/models` 返回 404 的历史实测，本轮未复验 |
| **OpenAI / zhipu / dashscope / volcengine 的实际可达性** | 无 key，只能验「地址能认出来」，不能验「调得通」 |
| **`ask` 的 `cancelled` 状态在真实对话里的表现** | 隔离测试覆盖了，线上只走到 answered 分支 |
| **后台任务服务重启后的 `exit_code`** | 设计上就是 `state=gone`（不知道成没成功），没有真实重启样本 |
| **`verification='passed'` 分支** | 生产至今没有「带证据的完成」样本（这是好事，不是缺陷） |

---

## 5. 今天的四次「评审判据不成立/我自己判断错」

留作参照，不是问责：

1. **B-6**：评审判据不成立（字段名记错），我没改注释。
2. **她报「task 带实参报 TypeError」**：核实是**过期的坏记忆**（错误发生在修复前的 18:04–18:06），让她重试即通过。
3. **我自己的 `list` 误判**（t18）：把 `pattern/recursive` 判成「被忽略」，实际是我没造出让参数产生差异的输入。
4. **我自己的 `progress` 崩溃**：单测用合成数据全绿，**真实台账里 `progress` 是列表**，一条脏字段打翻整张清单。

**共同点：结论都对，但都是「没有先去看真实数据」得出的。**
