---
description: "记录持久化类型更改及其兼容性确认。"
kind: persistence-change
---

# 2026-10-23-qq-role-source

[English](2026-10-23-qq-role-source.md) | 中文

## 概述

新增 qq-life 消息来源，为 Bot 所属 Agent 会话保留稳定输入标识和实际来源类别。

## 目录

- [声明](#declaration)
- [兼容性](#compatibility)
- [验证](#verification)
- [开发备注](#dev-note)

<a id="declaration"></a>
## 声明

```yaml persistence-change
schemaVersion: 1
id: 2026-10-23-qq-role-source
baseline: false
changes:
  - root: "event:agent/inbox/spliced"
    previous: "2026-10-13-user-rpc-principal"
    after: "9a12ce9fdf13b4873078461d4ee9351c122305632d538cfbbec16d0af3a61a79"
    decision: same-version
  - root: "event:developer/message"
    previous: "2026-10-13-user-rpc-principal"
    after: "cb6d4ef4c9169e7ccbaa6ccd72b4f94435f74d9c3314d90e39e40debfff4962d"
    decision: same-version
  - root: "event:session/title-llm-request"
    previous: "2026-10-13-user-rpc-principal"
    after: "666b085c2d8905f4b0bd2a7d0d8b316158ceb0444d2746f2aec68d4eb277150e"
    decision: same-version
  - root: "event:user/message"
    previous: "2026-10-13-user-rpc-principal"
    after: "00c701cfde6e61b49099afa82802fa1532ecec1e3e067dfd13ef64dd4b70c317"
    decision: same-version
```

<a id="compatibility"></a>
## 兼容性

Session 物理格式和原有来源变体不变。MessageSource 明确允许声明合并扩展，读取方对未知 kind 采用回落处理，不据此冒认为人类输入。新写入方沿用标准 user-message 信封并携带有区分标记的来源；内部唤醒标为 inference，不伪造真人陈述。新读取方仍接受原有来源。只有声明映射证明存在对应规范实现时，扫描才排除同目录生成的声明副本；手写声明继续检查。

<a id="verification"></a>
## 验证

SDK 角色会话测试 3 项通过，含首次输入持久化前读取事件；持久类型提取测试 86 项通过，含生成声明与手写声明区分。真实 DSH 组合测试接收合成输入、执行原生 read，通过 SessionQuery 投影持久来源，并仅交付一次正确随机标记。

<a id="dev-note"></a>
## 开发备注

无。
