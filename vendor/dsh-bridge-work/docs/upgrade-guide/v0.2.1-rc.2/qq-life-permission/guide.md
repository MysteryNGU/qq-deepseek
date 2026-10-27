---
kind: upgrade-guide
description: QQ bot experience protocol 2 replaces automatic peaks with delivered, single-use permission requests.
---

# QQ bot experience permission protocol 2

English | [中文](guide.zh.md)

## Change

Bot hosts negotiate `experience_protocol: 2` in `qq_life/hello`. Protocol 1 does not enable the experience tool. Ordinary SDK sessions are unaffected.

Crossing the experience threshold produces `awaiting_permission`. The role asks its own question through `life_say` with `experience_request_ref`. A confirmed delivery and a new explicit partner response are required for `permission.respond`; silence never grants permission. `stop` immediately resets the event and cancels pending activity. `exit` and `episode.end` use delayed settlement.

Continuous input requires explicit authorization and `authorization_mode: until_cancel`; the default is `one_shot`. Private completion uses isolated host state and never supplies permission to a shared event.

## Migration

1. Update the Python host and SDK worker together. Rebuild the worker and confirm `experience_protocol: 2` in the handshake before using `life_simulation`.
2. Load the current host tool catalog and `role_protocol`; use the current state revision and request identifier. Keep `experience_request_ref` in frozen expression plans and mark delivery only from transport receipts or reconciliation. Unknown delivery must not be resent under another identifier.
3. Bind `permission.respond` to the actual partner event after delivery. Keep responses single-use and preserve the stop barrier across callbacks, typing delays and restarts.
4. The Python host upgrades experience schema 1 to 2 by closing old activity without importing authorization. Start a new explicitly agreed event after migration. Schema-1-only code must not open a schema-2 database.
5. Verify one question, a silent timeout, one explicit permission, a stop during typing, and restart recovery before activating the updated worker. This source change does not restart an existing deployment.
