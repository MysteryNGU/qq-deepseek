---
kind: upgrade-guide
description: Bot 宿主须使用关系协议 2 和有界观察读取。
---

# QQ Bot 关系与资源协议

[English](guide.md) | 中文

## 变更

Bot 专用 SDK 会话在 `qq_life/hello` 声明 `relation_protocol: 2` 和 `resource_protocol: 1`。旧关系增量提案返回 `protocol_mismatch`。`include_full: true` 开始有界观察分页。普通 SDK 会话不受影响。

## 迁移

1. 更新 Bot 的反向生活回调，为上下文请求提供受众适用的 `stable_text`、动态 `text` 和已验证的 `resource_policy`；在提示词组装前应答 `static_context`。
2. 以方向、程度和置信度提交关系影响，同时提供 `relation_protocol: 2`、实际 `subject_actor_id` 和当前关系读取中的 `based_on_relationship_version`。主人下限和他人上限仍由宿主统一执行。
3. 使用观察的 `summary`、`search` 或 `lines` 读取并跟随 `next_cursor`。通过 `qq_life/session/original` 读取 spill 引用前，验证原事件的范围和遗忘状态。
4. 按稳定 `call_key` 和递增 revision 投影 `qq_life/session/usage`；最终累计样本替换早先样本。未知用量和缺失价格保持未知。重新构建并重启 SDK worker，验证握手、关系提交、有界结果读取与用量投影。
