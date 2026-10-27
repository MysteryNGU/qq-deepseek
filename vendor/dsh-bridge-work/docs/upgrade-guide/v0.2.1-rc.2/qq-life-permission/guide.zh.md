---
kind: upgrade-guide
description: QQ 机器人体验协议 2 用已送达、单次消费的许可请求替代自动峰值。
---

# QQ 机器人体验许可协议 2

[English](guide.md) | 中文

## 变更

机器人宿主在 `qq_life/hello` 协商 `experience_protocol: 2`。协议 1 不启用专项体验工具。普通 SDK 会话不受影响。

越过体验阈值只产生 `awaiting_permission`。角色通过带 `experience_request_ref` 的 `life_say` 亲口询问；只有实际确认送达和新的对方明确回应，才能提交 `permission.respond`。沉默不授权。`stop` 立即清空事件并取消待执行活动；`exit` 和 `episode.end` 延迟结算。

持续输入须有明确授权并提交 `authorization_mode: until_cancel`，默认 `one_shot`。私密完成只更新隔离的宿主状态，不能授权共享事件。

## 迁移

1. 同时更新 Python 宿主和 SDK 工作进程。重建工作进程，握手确认 `experience_protocol: 2` 后再使用 `life_simulation`。
2. 加载当前宿主工具目录和 `role_protocol`，使用当前状态版本和请求编号。在冻结表达中保留 `experience_request_ref`；仅凭传输回执或对账确认送达。发送结果未知时不能换编号重发。
3. `permission.respond` 必须绑定送达之后的真实对方消息。回应只消费一次；回调、打字延迟和重启都保留停止屏障。
4. Python 宿主将体验格式 1 升为 2 时关闭旧活动，不导入历史授权。迁移后须重新明确开启事件；仅支持格式 1 的代码不能打开格式 2 的数据库。
5. 启用更新后的工作进程前，验证一次询问、沉默超时、明确许可、打字期间停止及重启恢复。本次源码修改不重启已有部署。
