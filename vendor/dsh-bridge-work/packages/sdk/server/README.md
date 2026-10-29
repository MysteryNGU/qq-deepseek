---
description: "The stdio JSON-RPC serving plugin for deployments that let out-of-process SDK clients open sessions and drive agents in a DeepSeek Harness runtime."
kind: "package-reference"
---

# @deepseek-ai/dsh-sdk-jsonrpc-server

English | [中文](README.zh.md)

## Summary

`dsh-sdk-jsonrpc-server` serves the SDK wire protocol over stdio so out-of-process clients can drive harness agents: it opens one session per `sessionId`, queues user prompts, and streams every session event and agent status transition back to the client. Mount it as the `jsonrpc` plugin in a Loader composition; the surrounding tree supplies everything else — agents, model adapters, persistence, and tools. Stdout carries only JSON-RPC frames, so a deployment must not compose a stdout logger. It answers `shutdown` by disposing the root runtime and exiting 0; the app bin owns EOF and signal exits.

## Table of Contents

- [Use this package](#use-this-package)
- [Understand the implementation](#understand-the-implementation)
- [Further Exploration](#further-exploration)
- [Model Experience](#model-experience)
- [Known Limitations and Deferred Work](#known-limitations-and-deferred-work)
- [Dev Note](#dev-note)

-----

<a id="use-this-package"></a>
## Use this package

`session/resume` accepts a persisted `{ sessionId }` after initialization and returns `{}` once its Agent is ready. It loads history through `agents.resume()`, repairs an interrupted final turn, and preserves the stored origin and effective directory while applying the initialized model route. It sends no new input. Missing, unreadable, incompatible, or occupied sessions reject without creating a replacement. Prompts for an identity currently being restored wait for that restoration; shutdown drains pending restoration and releases its Agent. Historical events are retained in the log rather than streamed again as new notifications.

Concurrent requests for a persisted identity share the complete create-or-resume operation, including the persisted-session fallback. Bot expression slots, principal-owned public speech, stimulus accounting and publication eligibility belong to the bot host; its lifecycle tools return the actual activation receipt.

Bot-owned deployments can use `qq_life/session/input`, `input_status`, `events` and `control`. Identified inputs are fsynced before inbox admission, serialized by `(session_id,input_id)`, and reconciled against inbox and consumed-message history after a lost ACK. Inputs removed by a closed turn before consumption are eligible for readmission while later turns run; queued and consumed inputs remain deduplicated. The session generation survives transport reconnects. Text and encoded image blocks share the existing attachment admission path. Event pages preserve original sequence numbers; the host commits a continuous cursor and reports gaps. Attention cancellation preserves the inbox and resumes its first pending message after the current step stops. The adapter registers `life_read`, `life_remember`, `life_intent`, `life_appraise`, `life_change_self`, `life_say` and `life_rest`; their callbacks belong to the actual root Agent or its descendants. Only the host's scoped delivery callback sends QQ output. A final say/rest mixed with other tool calls is denied before execution; `concludeTurn()` does not cancel siblings. This deployment extension requires a bot-owned home and a host that implements the reverse life callbacks; it does not turn the standalone SDK into a bot.

The host selects one independent persona source for the conversation. Owner-private chats load the private source; other chats load the public source. `life_read(kind=self,persona_audience=private|public)` returns the selected text and file hash; the host denies private reads outside the owner-private chat. `life_change_self` targets `persona` and `persona_public` update the respective files without synchronization. The role chooses when an activity ends. `life_say` continues after sending by default, so one idle input can lead to multiple messages and actions. The host returns `turn_complete: true` only for a confirmed final expression (`end_turn: true`) or a reply-wait question; `life_rest` ends the turn. An unconfirmed delivery does not conclude the turn. The role chooses whether to act, notify or discuss. An optional `life_say.wait_for_reply` proposal asks the host to save a hold on one intention before sending the question; `life_intent` can also save that hold. The host enforces it for dependent actions and resumes only after the role interprets a new answer from the intended person. Notices, permissions and elapsed time do not imply an answer. Unrelated intentions remain available.

The bot handshake advertises `relation_protocol: 2` and `resource_protocol: 1`. Relationship proposals use directions, magnitudes and confidence against a host-read version; the host rejects legacy delta fields. Durable relationship notes have explicit replacement, resolution and revocation operations. The host owns policy, source checks and atomic state settlement.

Bot sessions expose paginated `qq_life/session/usage` and scoped `original` result retrieval. The adapter records each actual model stream separately, including compaction and failed attempts, and retains only the final cumulative usage sample. Host-configured text previews reuse the local spill store; missing recovery storage leaves the original result visible. Program values, images and audio keep their original semantics. Expired spill references fail explicitly. These extensions apply only to bound bot roots and their actual descendants. Runtime context refresh retires older snapshots only after the step has opened and the host has supplied a replacement, so persisted replacement records remain valid on restart.

`session/working-directory/get` accepts `{ sessionId }`; `session/working-directory/set` accepts `{ sessionId, path }`. Both return `{ cwd }` and create an unknown Session without starting a model turn. Reads recover missing directories through the working-directory service; changes preserve origin metadata and permissions, and reach the model through logged user context.

Mount this plugin when a runtime must serve SDK clients: add it to a `cordis.yml` that composes the agent service, boot the runtime, and clients connect over stdio. The common path is explicit — the plugin needs the `agents` service; every other capability comes from the surrounding tree.

The Bot adapter advertises `spending_protocol: 1`. Before each owned `llm/stream` attempt, including descendant and compaction requests, it calls the trusted host with `kind: budget_reserve` and the call identity, provider, model, admitted input references and resolved output limit. Only `ok: true` admits provider execution. An unreachable host also blocks the request. A final `budget_settle` callback supplies reported usage; failed or missing settlement retains the host's durable reservation and emits an error log. Price tables, calendar days, atomic admission and reservation recovery belong to the host. The presentation-only constructor without a budget callback continues to provide accounting without admission. This seam does not meter plugins making independent provider HTTP calls or non-token service charges.

Bot-owned hosts may negotiate `consult_protocol:1` to expose `life_consult` for asynchronous Codex conversations. The host validates scope, sources, admission and idempotency before returning a frozen execution spec. The adapter runs the bridge through an owned native `codex_consult` job, installs its completion collector before granting start, and returns admission while the role remains available. Cancellation targets that job; settlement awaits the managed process range. Final answers and restart recovery belong to the host’s durable input queue; a queued job or clean worker exit is not a complete consultation answer. This opt-in requires the jobs controller and subprocess provider in the deployment.

### Wiring

The plugin creates one agent per `sessionId` on first use. A registered model adapter wins the route; an unowned `deepseek-official` route mounts the DeepSeek adapter, and any other unowned provider fails initialization. The selected adapter resolves the exact model and optional reasoning effort before initialization succeeds.

### Configuration

| Field | Default | Meaning |
|---|---|---|
| `maxTokensAsSuccess` | `false` | Report max-token turn/subagent termination as a successful SDK result |

The profile composition owns each root agent's tools. `input`, `output`, and `exit` are runtime-only transport hooks for tests; production uses process stdio and `process.exit`. The generated [configuration catalog](../../../docs/config-catalog.md#deepseek-aidsh-sdk-jsonrpc-server) is the exhaustive source for every accepted field.

### stdout is the protocol

Stdout carries only JSON-RPC frames, so clients can parse every byte; diagnostics belong on stderr. Keep stdout loggers out of the composed tree.

### What SDK clients can do

`initialize` is the runtime-readiness boundary: when the server is mounted by a Loader composition, it waits for the current plugin tree to settle before replying, so async sibling capabilities such as initial MCP tool discovery are visible to the first prompt. The handshake returns the wire-stable identity `deepseek-harness-sdk-runtime`. The server validates the provider/model route and optional non-empty `reasoningEffort` through the selected adapter before it stores them; omission stores no effort, so the model retains its own default. An optional positive `maxTokens` becomes the request output cap of each SDK-created agent and its in-process descendants, while omission applies the selected adapter or provider route default. JSON-RPC requests may dispatch concurrently, so `session/prompt` rejects until one `initialize` has completed successfully; clients must await the handshake before sending prompts. An accepted prompt queues one identified user message and immediately returns `{ messageId }`; the server then streams every durable fact as `session.event` and every whole-agent lifecycle transition as `session.status`. It does not assign an assistant message or `turn/end` to a prompt, and independent requests may enqueue more work on the same session. Persistence roots and persona come from the surrounding composition.

`session/wait` observes an existing SDK-owned session through managed descendant completion and any resulting root turns. It rejects unknown or retired roots, never creates a session, and responds after the root stays idle across the descendant check. Idle children with parked inbox messages remain resident but do not delay this response; later waking input can resume them. Session notifications emitted before completion precede the response on the stdio transport. A live Agent failure remains associated with that exact session until a later committed `turn/end` accounts for the outcome; otherwise the wait rejects, including when the failure preceded the wait request. Errors already represented by a terminal remain in Session events, and later successful activity clears an earlier live failure.

### Shutdown and exit

The plugin answers `shutdown`, flushes the response, disposes the root context so SDK-owned agents, subscriptions, and persistence reach quiescence, then exits 0. EOF and signal exits belong to the app bin, which also disposes the root context. Unloading only this plugin stops serving without exiting the process.

-----

<a id="understand-the-implementation"></a>
## Understand the implementation

The negotiated QQ life `compact_tool_protocol:1` uses Python-owned structured proposal schemas and six base tools. The trusted host may also select registered `default_tools` for the current audience; the QQ host selects `pwsh`, `job_output` and `job_kill` only for owner-private chat. These definitions enter the first request and avoid separate discovery/load requests and mid-task schema changes, at the cost of a larger initial tool list. Defaults grant no execution permission and do not expand descendant or internal-memory tool lists. Exact capability reads with `ref`, `schema_hash` and `load:true` stage other complete current schemas. Loaded definitions are journaled as attribution-only developer messages, revalidated after resume, and still pass the original host execution checks. Full versioned documentation remains available through exact catalog reads and schema pagination. Without negotiation, the existing complete tool presentation remains active.

Bot request preparation uses the shared token meter after the final header, runtime facts and current inputs are logged. It applies absolute chat/task soft targets, versioned read-pair pruning, persistent image offload, and at most one existing DSH handoff attempt per request. Handoffs require a complete tool-balanced range larger than both twice the summary target and the recent-history allowance. Later steps in the same turn may summarize newly accumulated history; unchanged failed ranges are not retried. Host-certified resolved `input_pending` and `acknowledgement_required` errors, including rejected shell calls, may join that range; other errors and unknown operations remain protected. Original events remain in the durable log. Required inputs may exceed the soft target; known physical-capacity failures are explicit. Unknown capacity and adapter-dependent estimates remain labelled. The two latest image batches and source-backed pins remain available; active tasks conservatively retain their image references. Internal memory sessions keep their existing workflow.

After complete request assembly, successful host validation supplies a durable batch reference for consumed unresolved inputs. The SDK logs that batch before the provider request and retires its previous presentation; originals remain replayable. The model explicitly submits `handled_batch_ref` with its next say/rest, including a work notice, or uses individual IDs for partial acknowledgement. The host expands only that frozen batch; unread and later inputs remain unresolved and keep their execution and delivery checks. Confirmed work acknowledgements match source input IDs, audience and forget epoch, so internal updates do not erase an existing acknowledgement; new user inputs require their own confirmed response. Owner-private instructions group independent read-only checks into one shell call while preserving dependent operations and progress messages. Dynamic simulation coverage shows unresolved items and processed counts for the current audience; `life_read(kind:simulation,view:input_coverage)` retrieves versioned detail pages. Full simulation reads and execution fences retain their existing behavior.

Image reads and owner-local image imports enqueue verified pixels through the existing same-session image admission path. The read receipt identifies that visual input; metadata alone is not visual inspection. The QQ host prefers platform-resolved files within configured media roots and reads the public QQ CDN directly without inheriting a Windows system proxy, while retaining HTTPS verification and audience checks. Batch presentations add a bounded current-input section near the request tail; retiring older presentations changes that tail and does not erase original input events.

<details>
<summary>Implementation internals — click to expand</summary>

This section explains the design behind the serving plugin; the observable behavior is fully covered in [Use this package](#use-this-package).

### Design concept

The plugin is a thin presentation adapter: [`HarnessSdkJsonRpcServer`](src/server.ts) owns the protocol methods and notifications, while the transport and the named wire types come from `dsh-sdk-protocol`, shared with the client SDKs. It subscribes to session, agent, and subagent lifecycle events and forwards them as wire notifications; subagent completions are forwarded only when the service-snapshotted lifecycle `local` flag is true — provider names, child ids, and durable lineage never establish locality.

### Source map

| File | Role |
|---|---|
| [`src/index.ts`](src/index.ts) | Plugin entry: `Config` schema, stdio wiring, request dispatch, shared shutdown/exit task |
| [`src/server.ts`](src/server.ts) | `HarnessSdkJsonRpcServer`: protocol methods, per-session agent creation, lifecycle subscriptions, teardown |

### Request flow

Each protocol method validates its inputs and resolves the owning state before acting — `initialize` stores the SDK route, `session/prompt` resolves the live agent+session pair and queues the message, and `shutdown` flushes the response, then disposes the root context to quiescence before exiting 0 — and a shared exit task guarantees that racing `shutdown` requests never dispose or exit twice. The dispatch lives in [src/index.ts](src/index.ts) and [src/server.ts](src/server.ts).

### Teardown

`server.shutdown()` closes descendant admission below its exact root Agents and drains their managed descendants child-first before releasing the root handles and adapter. It removes its subscriptions while unrelated parent trees and the surrounding context stay running when just this plugin is unloaded. A descendant drain failure is reported after the remaining root and adapter teardown completes. Protocol `shutdown` instead disposes the root fiber so persistence and the whole runtime reach quiescence before the process exits.

</details>

-----

<a id="further-exploration"></a>
## Further Exploration

Read these pages when the plugin contract is not enough. They move from the wire protocol to the clients and the runnable application.

- [SDK wire protocol](../protocol/README.md) — the methods and payload shapes this plugin serves.
- [TypeScript SDK client](../client/README.md) — the client that drives this plugin.
- [SDK application bundle](../../bundle/sdk-app/README.md) — the `dsh --profile sdk` application that boots this plugin.
- [Python SDK](../../../python/README.md) — the Python client that drives the same server.
- [SDK runtime distribution decision](../../../.agents/notes/implemented/architecture/2026-07-10-single-file-executable-sdk-runtime-distribution.md) — why the packaged runtime serves a closed plugin tree.

-----

<a id="model-experience"></a>
## Model Experience

### SDK user message

#### What the model sees

For each accepted `session/prompt`, text and durable content references enter one user message verbatim. Inline `SdkEncodedImageBlock` values are validated and committed through the composition's attachment store first, so the session log retains content-addressed image references rather than base64 bytes. Ordinary SDK sessions use the surrounding composition's prompt and tools. Bound bot sessions additionally receive audience-specific stable role rules, a complete dynamic snapshot and life tools; both rules and snapshots are logged. Negotiated `capability_protocol:1` and `media_adapter_protocol:1` advertise the corresponding additive tool parameters. The Bot catalog reads an already live, generation-bound Agent; its local method provider is scoped to that Agent, rechecks content hashes on loading, and unregisters on disposal. Tool policy identity comes from the registered definition, never model arguments. The `memory_protocol:1` handshake supports same-role internal memory work when the host context sets `processing_mode: internal`. That Agent exposes only `life_read`, `life_remember` and `life_rest`; an execution guard also blocks other tools registered locally. The mode stays fixed for that Agent, so switching mode requires a new Session. A successful summary response with `turn_complete: true` ends the turn without another model request.

#### Token effect

Data-dependent user-message tokens enter retained session history and are resent on later turns until another package compacts them. In Bot-owned sessions, only the latest runtime-context snapshot enters each ordinary request. A prepended pre-step listener retires older snapshots before automatic compaction; successful context refresh retires the remaining snapshot before the loop appends its replacement. Failed refresh retains the last valid snapshot. Logged per-node replacements preserve original events, user inputs, tool calls and results; adjacent `compaction/prune` records update token pressure. Empty replacement nodes produce no model messages. Restored histories follow the same rule, and unbound sessions are unchanged. The token-meter service is required when retiring snapshots. Method summaries use the native skill catalog; bodies load on demand through `skill`. An unchanged catalog is not injected again. The JSON-RPC frames, session notifications, and server bookkeeping add zero model-context tokens.

#### KV Cache effect

Ordinary prompt inputs append after the reusable request prefix. Bot dynamic snapshots are logged user-context updates, with `runtime_context_policy: latest-only-v1` advertised by the handshake. Retiring a prior snapshot changes the prefix from that position, so the first cleanup of a long legacy session can reduce cache hits. Subsequent requests retain the earlier conversation prefix while replacing the latest state near the tail; a changed audience or rule version can replace the stable role-rule section. Larger historical handoffs and smaller dynamic coverage reduce retained input, while each handoff changes the prefix and may cause a cold request; the per-request bound limits repeated changes. Provider-reported cache usage, rather than the section API alone, establishes the actual cache benefit.

## Known Limitations and Deferred Work

QQ-life hosts may negotiate `experience_protocol:1` to expose `life_simulation` for an explicitly confirmed adult intimate scene. The host owns input assessment, persistent state, control revisions and transition receipts; the SDK forwards proposals and records tool results. Internal memory sessions retain their read/remember/rest allowlist and cannot control simulation. Missing negotiation exposes no simulation tool. Tick integration does not request a model; the role reads committed transitions before composing a reaction. This protocol adds no Session event discriminator or persistence format change.

<a id="known-limitations-and-deferred-work"></a>


These limits define when the plugin needs special operational care. They are current package constraints, not a comparison with other serving approaches or a task backlog.

- **The wire has no per-session close or prompt-cancel method** — SDK-created agents remain live until process shutdown.
- **There is no per-prompt result** — `MessageId` identifies inbox admission only; clients that own an automation interval must define and observe that interval themselves.
- **stdout purity is deployment-enforced** — a surrounding config can still load a stdout logger and corrupt the JSON-RPC channel; this plugin does not inspect or veto sibling loggers.
- **Automatic adapter mounting is DeepSeek-specific** — `initialize` can reuse any pre-registered model adapter, but its only fallback mounts the DeepSeek adapter.

<a id="dev-note"></a>
### Dev Note

<details>
<summary>Working context for maintainers — click to expand</summary>

This Dev Note is working context for maintainers and is explicitly non-authoritative — shipped behavior and limits live in the sections above and in the code. The single-executable runtime distribution pairs this plugin with the packaged `jsonrpc-demo` bin; keep the shutdown/exit contract consistent with the app bin, which owns EOF and signal exits. No other unresolved design questions are recorded.

</details>
