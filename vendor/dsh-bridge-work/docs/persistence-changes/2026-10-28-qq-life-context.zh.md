---
description: "记录持久化类型更改及其兼容性确认。"
kind: persistence-change
---

# 2026-10-28-qq-life-context

[English](2026-10-28-qq-life-context.md) | 中文

## 概述

新增仅作归属标记的 qq-life-tools 与 qq-life-projection 消息来源，用于可恢复的工具呈现、保留来源的可选上下文与已验证读取引用。

## 目录

- [声明](#declaration)
- [兼容性](#compatibility)
- [验证](#verification)
- [开发备注](#dev-note)

<a id="declaration"></a>
## 声明

```yaml persistence-change
schemaVersion: 1
id: 2026-10-28-qq-life-context
baseline: false
changes:
  - root: "event:agent/inbox/spliced"
    previous: "2026-10-23-qq-role-source"
    after: "3a2c1a872aebc4f6d06eb6381f0ddb7c4dc99fc5a96fec245654657cbf3db8e5"
    decision: same-version
  - root: "event:developer/message"
    previous: "2026-10-23-qq-role-source"
    after: "c848ef3a7984c106a6bf37239277db757084c400a16be066523bb155d536d920"
    decision: same-version
  - root: "event:session/title-llm-request"
    previous: "2026-10-23-qq-role-source"
    after: "ef81d4eefce036249e6c234140c00ff750068584a94833076a69f108bf7523d6"
    decision: same-version
  - root: "event:user/message"
    previous: "2026-10-23-qq-role-source"
    after: "bdac1548b5ecec710f977e2df34fbbdfbeb5b42bb921ed647817bb598694afdc"
    decision: same-version
```

<a id="compatibility"></a>
## 兼容性

保持 Session 版本。已有用户和 developer 消息继续有效。旧读取器保留未知来源种类及 JSON 元数据，developer 正文仍为普通文本或空正文。新读取器只有核对当前受众注册表与 schema hash 后才恢复工具呈现；缺少元数据时仍有基础工具和既有能力目录。两种来源均不授予执行权限，不改变已有事件含义。既有 prune 和 image/offload 事件结构不变。

<a id="verification"></a>
## 验证

无密钥 Vitest qq-life-sessions.spec.ts 与 qq-life-context.spec.ts 共 41 项通过，覆盖加载失败、适配器重启、定义变更、图片退出持久重放及读取成对处理。Python 上下文、资源、能力、人物与记忆和费用回归选集 129 项通过。

<a id="dev-note"></a>
## 开发备注

无。
