---
description: "Records a persistence type transition and its compatibility acknowledgement."
kind: persistence-change
---

# 2026-10-28-qq-life-context

English | [中文](2026-10-28-qq-life-context.zh.md)

## Summary

Adds attribution-only qq-life-tools and qq-life-projection message sources for recoverable tool presentation, source-preserving optional context and verified-read references.

## Table of Contents

- [Declaration](#declaration)
- [Compatibility](#compatibility)
- [Verification](#verification)
- [Dev Note](#dev-note)

<a id="declaration"></a>
## Declaration

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
## Compatibility

Same Session version. Existing user/developer messages remain valid. Older readers preserve unknown source kinds and JSON metadata; developer content remains ordinary text or empty content. New readers restore tool presentation only after checking the current scoped registry and schema hashes. Missing metadata leaves base tools and the existing catalog available. Neither kind grants execution authority or changes the interpretation of existing events. Existing prune and image/offload event payloads are unchanged.

<a id="verification"></a>
## Verification

Keyless Vitest qq-life-sessions.spec.ts and qq-life-context.spec.ts: 41 tests passed, including failed loads, adapter restart, schema change, persistent image offload and verified-read pairing. Python context, resource, capability, person and memory/spending regression selection: 129 tests passed.

<a id="dev-note"></a>
## Dev Note

None.
