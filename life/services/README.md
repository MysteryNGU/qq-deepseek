# DSH 接入与编辑原语

这里保留 DSH 会话传输桥、Windows pnpm 适配、DSH 工坊及其补丁匹配原语。实际自我修改的持久入口是 `life/self_change.py` 与 `life/dsh_changes.py`，Bot 代码发布由 `life/release.py` 和独立监督器执行。

`store.py/schema.py` 仅保存被动桥接与工坊组件测试需要的操作回执，不再创建旧意图、审批、发送或思考表。正式角色使用 `RoleAgent` 与同一个 Repository，不组装 ServiceHub。

旧 Python 工具目录、审批分发、Outbox、思考器及 GUI 认知路径已删除。能力由当前 DSH profile 的实际工具目录提供，模型自行发现、调用和验证；这里不再维护一份平行工具 schema。
