---
kind: upgrade-guide
description: Bot hosts must use relationship protocol 2 and bounded observation reads.
---

# QQ bot relationship and resource protocols

English | [中文](guide.zh.md)

## Change

Bot-owned SDK sessions advertise `relation_protocol: 2` and `resource_protocol: 1` in `qq_life/hello`. Legacy relationship delta proposals fail with `protocol_mismatch`. `include_full: true` starts bounded observation pagination. Ordinary SDK sessions are unaffected.

## Migration

1. Update the bot's reverse life callbacks to return audience-specific `stable_text`, dynamic `text` and validated `resource_policy` for context requests; answer `static_context` before prompt assembly.
2. Submit relationship directions, magnitudes and confidence with `relation_protocol: 2`, actual `subject_actor_id` and `based_on_relationship_version` from a current relationship read. Keep the host's owner floor and other-person ceiling authoritative.
3. Use observation `summary`, `search` or `lines` reads and follow `next_cursor`. Validate the original event's scope and deletion state before retrieving its spill reference through `qq_life/session/original`.
4. Project `qq_life/session/usage` by stable `call_key` and increasing revision; final cumulative samples replace earlier samples. Unknown usage and missing prices stay unknown. Rebuild and restart the SDK worker, then verify the handshake, a relationship commit, bounded result retrieval and usage projection.
