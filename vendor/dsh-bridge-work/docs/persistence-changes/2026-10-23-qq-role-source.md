---
description: "Records a persistence type transition and its compatibility acknowledgement."
kind: persistence-change
---

# 2026-10-23-qq-role-source

English | [中文](2026-10-23-qq-role-source.zh.md)

## Summary

Adds a qq-life message source carrying a stable input identity and its actual origin for bot-owned Agent sessions.

## Table of Contents

- [Declaration](#declaration)
- [Compatibility](#compatibility)
- [Verification](#verification)
- [Dev Note](#dev-note)

<a id="declaration"></a>
## Declaration

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
## Compatibility

The physical Session format and existing source variants remain unchanged. MessageSource is explicitly merge-extensible: readers fall through unknown kinds rather than interpreting them as human input. The new producer retains the standard user-message envelope and a tagged source; internal wakes use inference origin rather than a fabricated human report. New readers continue accepting all old sources. Proven sibling declaration build output is excluded from discovery only when its declaration map identifies an existing canonical implementation; authored declarations remain checked.

<a id="verification"></a>
## Verification

SDK role session tests: 3 passed, including durable event queries before the first input admission; persistence schema extraction tests: 86 passed, including emitted versus authored declaration discovery. Actual DSH composition smoke admitted a synthetic input, executed native read, projected the persisted source through SessionQuery and delivered the correct random marker once.

<a id="dev-note"></a>
## Dev Note

None.
