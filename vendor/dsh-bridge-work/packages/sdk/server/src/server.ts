/**
 * JSON-RPC methods and notifications for out-of-process harness SDKs.
 * The surrounding context owns plugins, persistence, and configured adapters.
 *
 * @module @deepseek-ai/dsh-sdk-jsonrpc-server/server
 */

import type { Context } from '@deepseek-ai/cordis'
import type { ApprovalRequestEvent, ApprovalOutcome } from '@deepseek-ai/dsh-user-approval/types'
import { readFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { join, resolve } from 'node:path'
import { brandString } from '@deepseek-ai/dsh-brand'
import type { Agent, AgentHandle } from '@deepseek-ai/dsh-agent'
import { admitEncodedImages, type EncodedImageAttachment, type ImageAttachmentRef } from '@deepseek-ai/dsh-attachment'
import { createUserMessage, ReasoningEffortId, type ContentBlock, type LlmRuntime } from '@deepseek-ai/dsh-llm'
import { carrierKeyOf, type Scoped } from '@deepseek-ai/dsh-scope'
import type { SessionId, SessionLogOffset } from '@deepseek-ai/dsh-session'
import type {} from '@deepseek-ai/dsh-working-directory'
import type SubagentRuntime from '@deepseek-ai/dsh-subagent'
import type { SubagentRunEndInfo, SubagentRunInfo } from '@deepseek-ai/dsh-subagent'
import { isSettledStatus, QqLifeOperationLog, requestHash,
  type QqLifeOperation } from './qq-life-operations.ts'
import * as LlmDeepSeek from '@deepseek-ai/dsh-llm-deepseek-api-key'
import { QqLifeSessions, QQ_ROLE_ADAPTER_VERSION } from './qq-life-sessions.ts'
import type {
  InitializeParams,
  InitializeResult,
  JsonRpcTransportPeer,
  SessionEventNotification,
  SessionPromptParams,
  SessionPromptResult,
  SessionResumeParams,
  SessionWaitParams,
  SessionWorkingDirectoryParams,
  SessionWorkingDirectorySetParams,
  SessionWorkingDirectoryResult,
  SdkEncodedImageBlock,
  SubagentFinishedNotification,
  SubagentStartedNotification,
} from '@deepseek-ai/dsh-sdk-protocol'

interface SessionRecord {
  handle: AgentHandle
  /** Latest live failure not superseded by a subsequently committed terminal. */
  failure: { readonly error: unknown; readonly atOffset: SessionLogOffset } | undefined
}

function encodedImage(block: SessionPromptParams['contentBlocks'][number]): block is SdkEncodedImageBlock {
  return block.type === 'image' && 'data' in block
}

async function durablePromptContent(ctx: Context, blocks: SessionPromptParams['contentBlocks']): Promise<ContentBlock[]> {
  const images = blocks.filter(encodedImage)
  if (images.length === 0) return blocks as ContentBlock[]
  const attachments = ctx.get('attachments')
  if (attachments === undefined) throw new Error('SDK image prompt requires an attachment store')
  const refs = await admitEncodedImages(attachments, images.map((image): EncodedImageAttachment => ({
    data: image.data,
    mediaType: image.mimeType,
  })))
  let next = 0
  return blocks.map(block => encodedImage(block)
    ? { type: 'image', attachment: refs[next++] as ImageAttachmentRef }
    : block)
}

/** Recover the delegating parent from the service-owned scoped carrier. */
function subagentParentOf(carrier: Scoped<SubagentRuntime>): Agent {
  return carrierKeyOf(carrier) as Agent
}

/** Deployment-specific status mapping for SDK turn and subagent outcomes. */
export interface HarnessSdkJsonRpcServerOptions {
  /** Report max-token termination as an accepted result instead of an infrastructure error. */
  maxTokensAsSuccess?: boolean
}

function successStatus(reason: string, options: HarnessSdkJsonRpcServerOptions): 'ok' | 'error' {
  if (reason === 'completed') return 'ok'
  return reason === 'max-tokens' && options.maxTokensAsSuccess === true ? 'ok' : 'error'
}

/**
 * SDK server over one booted harness context and transport peer. Construction
 * subscribes to session, agent, and subagent lifecycle events until shutdown;
 * reinitialization is unsupported.
 */
export class HarnessSdkJsonRpcServer {
  private roleCapabilityProtocol = false
  private roleMediaAdapterProtocol = false
  private roleConsultProtocol = false
  private roleExperienceProtocol = false
  private cwd = process.cwd()
  private provider = 'deepseek-official'
  private model = 'deepseek-official'
  private reasoningEffort: ReturnType<typeof ReasoningEffortId> | undefined
  private maxTokens: number | undefined
  private llmFiber: { dispose(): Promise<void> } | undefined
  private readonly sessions = new Map<string, SessionRecord>()
  private readonly sessionCreations = new Map<string, Promise<SessionRecord>>()
  private readonly disposers: (() => void)[] = []
  private shutdownTask: Promise<Record<string, never>> | undefined
  private qqLifeLog: QqLifeOperationLog | undefined
  private readonly qqLifeInflight = new Map<string, Promise<void>>()
  private readonly qqLifeCancelRequested = new Set<string>()
  private readonly qqLifeStartedAt = Date.now()
  private roleSessions: QqLifeSessions | undefined
  private roleContinuityEnabled = false
  private shuttingDown = false
  private initialized = false

  constructor(
    private readonly ctx: Context,
    private readonly transport: JsonRpcTransportPeer,
    private readonly options: HarnessSdkJsonRpcServerOptions = {},
  ) {
    const serverOptions = this.options
    const roleSessionOwner = () => this.roleSessions
    this.disposers.push(ctx.on('approval/request', async (request: ApprovalRequestEvent, next): Promise<ApprovalOutcome> => {
      if (this.sessions.get(String(request.agent.session.id))?.handle.agent !== request.agent) return next()
      const operation = this.qqLifeLog?.all().find(item =>
        item.sessionId === String(request.agent.session.id) && this.qqLifeInflight.has(item.operationId)
        && (request.callId === undefined || String(request.callId) === item.operationId))
      if (operation === undefined) return next()
      if (request.signal?.aborted) return 'cancelled'
      try {
        const response = await this.transport.request('qq_life/approval', {
          operationId: operation.operationId, executionScope: operation.sessionId,
          toolName: request.toolName, callId: request.callId,
          reason: request.reason, arguments: operation.arguments,
        }) as { outcome?: unknown }
        if (request.signal?.aborted) return 'cancelled'
        return response?.outcome === 'allowed-once' ? 'allowed-once' : 'rejected'
      } catch {
        return 'unavailable'
      }
    }))
    this.disposers.push(ctx.on('session/event', (session, event) => {
      const record = this.sessions.get(session.id)
      if (event.type === 'turn/end' && record?.handle.agent.session === session
        && record.failure !== undefined && event.seq >= record.failure.atOffset) {
        record.failure = undefined
      }
      const payload: SessionEventNotification = { sessionId: String(session.id), event }
      this.transport.notify('session.event', payload)
    }))
    this.disposers.push(ctx.on('agent/error', ({ agent, error }) => {
      const record = this.sessions.get(agent.session.id)
      if (record?.handle.agent !== agent) return
      record.failure = { error, atOffset: agent.session.seq }
    }))
    this.disposers.push(ctx.on('agent/status', ({ agent, status }) => {
      this.transport.notify('session.status', { sessionId: String(agent.session.id), status })
    }))
    this.disposers.push(ctx.on('session/created', (session) => {
      const parentSession = session.header.parentSession
      if (parentSession === undefined) return
      const parentCallId = this.roleSessions?.captureChildCall(String(parentSession), String(session.id))
      const payload: SubagentStartedNotification = {
        parentSessionId: String(parentSession),
        childSessionId: String(session.id),
        ...(parentCallId === undefined ? {} : { parentCallId }),
      }
      this.transport.notify('subagent.started', payload)
    }))
    this.disposers.push(ctx.on('subagent/start', function (this: Scoped<SubagentRuntime>, info: SubagentRunInfo) {
      if (!info.local) return
      const parent = subagentParentOf(this)
      const parentCallId = roleSessionOwner()?.captureChildCall(String(parent.session.id), String(info.id))
      if (parentCallId === undefined) return
      transport.notify('qq_life/subagent.started', {
        parentSessionId: String(parent.session.id), childSessionId: String(info.id), parentCallId, runId: String(info.runId),
      })
    }))
    this.disposers.push(ctx.on('subagent/end', function (this: Scoped<SubagentRuntime>, info: SubagentRunEndInfo) {
      const parent = subagentParentOf(this)
      // This protocol reports only in-process child sessions. The service
      // snapshots the provider name and local flag through child disposal;
      // matching ids or parent lineage alone never establishes locality.
      if (!info.local) return
      const payload: SubagentFinishedNotification = {
        provider: info.provider,
        agentId: String(info.id),
        parentSessionId: String(parent.session.id),
        childSessionId: String(info.id),
        status: successStatus(info.stopReason, serverOptions),
        stopReason: info.stopReason,
        ...(info.lastAssistantMessage === undefined
          ? {}
          : { lastAssistantMessage: [...info.lastAssistantMessage] }),
      }
      transport.notify('subagent.finished', payload)
      if (roleSessionOwner()?.ownsChildCall(String(info.id))) {
        transport.notify('qq_life/subagent.finished', { ...payload, runId: String(info.runId) })
      }
    }))
  }

  /**
   * Validate and configure the SDK route, mounting the DeepSeek fallback only when unowned.
   * @param params - SDK handshake parameters.
   * @returns server identity for the handshake.
   */
  async initialize(params: InitializeParams): Promise<InitializeResult> {
    if (params.reasoningEffort !== undefined
      && (typeof params.reasoningEffort !== 'string' || params.reasoningEffort.length === 0)) {
      throw new TypeError('initialize reasoningEffort must be a non-empty string')
    }
    if (params.maxTokens !== undefined
      && (!Number.isSafeInteger(params.maxTokens) || params.maxTokens <= 0)) {
      throw new TypeError('initialize maxTokens must be a positive safe integer')
    }
    const cwd = resolve(params.cwd)
    const provider = params.provider
    const model = params.model
    const reasoningEffort = params.reasoningEffort === undefined
      ? undefined
      : ReasoningEffortId(params.reasoningEffort)
    if (!this.hasAdapterFor(provider)) {
      if (provider !== 'deepseek-official') throw new Error(`no adapter registered for provider "${provider}"`)
      this.llmFiber = await this.ctx.plugin(LlmDeepSeek)
    }
    // Adapter presence was read from this service above; a successful fallback mount also requires it.
    const llm = this.ctx.get('llm') as LlmRuntime
    await llm.resolveCallConfig({
      provider,
      model,
      ...reasoningEffort === undefined ? {} : { reasoningEffort },
      ...params.maxTokens === undefined ? {} : { maxTokens: params.maxTokens },
    })
    this.cwd = cwd
    this.provider = provider
    this.model = model
    this.reasoningEffort = reasoningEffort
    this.maxTokens = params.maxTokens
    this.initialized = true
    return { serverInfo: { name: 'deepseek-harness-sdk-runtime', version: '0.0.1' } }
  }

  /**
   * Queue one identified prompt without assigning later activity to it.
   * @param params - target session and user content.
   * @returns the durable message identity.
   */
  async prompt(params: SessionPromptParams): Promise<SessionPromptResult> {
    if (!this.initialized) throw new Error('SDK server is not initialized')
    const rec = await this.getOrCreateSession(params.sessionId)
    // An agent-loop-only reload disposes the loop's agents while this record
    // survives; a retained agent accepts followup() silently, so validate the
    // record against the live registry before delivery.
    this.assertLiveAgent(rec, params.sessionId)
    const content = await durablePromptContent(this.ctx, params.contentBlocks)
    // Attachment admission crosses an async boundary where shutdown or an
    // agent-loop reload may detach the retained handle.
    this.assertLiveAgent(rec, params.sessionId)
    const message = createUserMessage({
      content,
      source: { kind: 'user' },
    })
    rec.handle.agent.followup(message)
    return { messageId: message.id }
  }

  /**
   * Restore a persisted session and repair its interrupted final turn without sending input.
   * Uses the initialized model route while preserving the stored session identity and directory.
   * Rejects missing, unreadable, or already owned sessions; never creates a replacement.
   * @param params - persisted session identity in the configured persistence store.
   * @returns an empty result after the Agent is ready for prompts.
   */
  async resume(params: SessionResumeParams): Promise<Record<string, never>> {
    if (!this.initialized) throw new Error('SDK server is not initialized')
    if (this.shuttingDown) throw new Error('SDK server is shutting down')
    const sessionId = params.sessionId
    if (this.sessions.has(sessionId) || this.sessionCreations.has(sessionId)) {
      throw new Error(`SDK session is already open or opening: ${sessionId}`)
    }
    await this.openSession(sessionId, true)
    return {}
  }

  /**
   * Wait for an existing owned Agent and its managed descendants to finish.
   * The response follows all Session notifications emitted during the wait.
   * Rejects a live Agent failure that has no subsequent durable terminal.
   * @param params - the SDK-owned session to observe without creating one.
   * @returns an empty result after the root stays idle across the descendant check.
   */
  async wait(params: SessionWaitParams): Promise<Record<string, never>> {
    if (!this.initialized) throw new Error('SDK server is not initialized')
    const rec = this.sessions.get(params.sessionId)
    if (rec === undefined) throw new Error(`unknown SDK session: ${params.sessionId}`)
    const agent = rec.handle.agent
    const subagents = this.ctx.get('subagents')
    while (true) {
      this.assertLiveAgent(rec, params.sessionId)
      await agent.whenIdle()
      const idleSeq = agent.session.seq
      const children = await subagents?.waitForChildren(agent)
      this.assertLiveAgent(rec, params.sessionId)
      if (!children && agent.status === 'idle' && agent.session.seq === idleSeq) {
        if (rec.failure !== undefined) throw rec.failure.error
        return {}
      }
    }
  }

  private assertLiveAgent(rec: SessionRecord, sessionId: string): void {
    if (this.ctx.agents.get(rec.handle.agent.id) !== rec.handle.agent) {
      throw new Error(`session agent was disposed outside the server: ${sessionId}`)
    }
  }

  /**
   * Read a Session's effective directory, recovering a missing directory to its origin.
   * @param params - target Session; an unknown id creates it.
   * @returns the absolute effective directory.
   */
  async getWorkingDirectory(params: SessionWorkingDirectoryParams): Promise<SessionWorkingDirectoryResult> {
    const agent = await this.workingDirectoryAgent(params.sessionId)
    return { cwd: await this.ctx.workingDirectory.ensure(agent) }
  }

  /**
   * Change a Session's directory and queue its model-visible transition.
   * @param params - target Session and absolute or current-directory-relative path.
   * @returns the validated absolute directory; origin and permissions are unchanged.
   */
  async setWorkingDirectory(params: SessionWorkingDirectorySetParams): Promise<SessionWorkingDirectoryResult> {
    const agent = await this.workingDirectoryAgent(params.sessionId)
    return { cwd: await this.ctx.workingDirectory.set(agent, params.path) }
  }

  private async workingDirectoryAgent(sessionId: string): Promise<Agent> {
    if (!this.initialized) throw new Error('SDK server is not initialized')
    const rec = await this.getOrCreateSession(sessionId)
    this.assertLiveAgent(rec, sessionId)
    return rec.handle.agent
  }

  /**
   * Dispose server-owned agents, adapter, and subscriptions to quiescence.
   * Managed descendants drain before the root Agent handles and adapter are released.
   * The surrounding context remains running.
   * @returns empty JSON-RPC result.
   */
  shutdown(): Promise<Record<string, never>> {
    this.shutdownTask ??= this.performShutdown()
    return this.shutdownTask
  }

  private async performShutdown(): Promise<Record<string, never>> {
    this.shuttingDown = true
    const pendingCreations = [...this.sessionCreations.values()]
    await Promise.allSettled(pendingCreations)
    this.sessionCreations.clear()
    const records = [...this.sessions.values()]
    this.sessions.clear()
    const failures: unknown[] = []
    while (this.disposers.length > 0) {
      try {
        this.disposers.pop()?.()
      } catch (error) {
        failures.push(error)
      }
    }
    try {
      await this.ctx.get('subagents')?.drainDescendants(records.map(rec => rec.handle.agent))
    } catch (error: unknown) {
      failures.push(error)
    }
    const teardownResults = await Promise.allSettled([
      ...records.map(rec => Promise.resolve().then(() => rec.handle.dispose())),
      ...(this.llmFiber === undefined ? [] : [Promise.resolve().then(() => this.llmFiber?.dispose())]),
    ])
    this.llmFiber = undefined
    this.roleSessions?.dispose()
    this.roleSessions = undefined
    failures.push(...teardownResults
      .filter((result): result is PromiseRejectedResult => result.status === 'rejected')
      .map(result => result.reason as unknown))
    if (failures.length === 1) throw failures[0]
    if (failures.length > 1) throw new AggregateError(failures, 'SDK server teardown failed')
    return {}
  }

  /** Absolute Harness home of this worker: DSH_HOME wins, else <profile>/../.. . */
  private qqLifeHome(): string {
    const configured = process.env['DSH_HOME']
    if (configured !== undefined && configured.trim().length > 0) return resolve(configured)
    const profileDir = this.qqLifeProfileContext()?.dir
    return profileDir === undefined ? join(homedir(), '.dsh') : resolve(profileDir, '..', '..')
  }

  /** Profile the loader actually booted in this process, if any. */
  private qqLifeProfileContext(): { name?: string; dir?: string } | undefined {
    return this.ctx.get('profileContext') as { name?: string; dir?: string } | undefined
  }

  /** Durable operation ledger, read once so a restart reconciles instead of repeating. */
  private qqLifeStore(): QqLifeOperationLog {
    if (this.qqLifeLog !== undefined) return this.qqLifeLog
    const log = new QqLifeOperationLog(join(this.qqLifeHome(), 'qq-life', 'operations.jsonl'))
    const recovered = log.load()
    if (recovered > 0) {
      process.stderr.write(`dsh: qq-life 对账 ${recovered} 条未结束操作，标记为 unknown\n`)
    }
    this.qqLifeLog = log
    return log
  }

  /**
   * Prove which profile and home this worker really runs in, and whether the
   * bot's own ownership marker is present — never a constant.
   */
  private qqLifeIdentity(): { name: string; dir?: string; home: string; ownedByBot: boolean; reason?: string } {
    const home = this.qqLifeHome()
    const profileContext = this.qqLifeProfileContext()
    const name = profileContext?.name ?? '(no-profile)'
    const dir = profileContext?.dir
    if (dir === undefined || dir.length === 0) {
      return { name, home, ownedByBot: false, reason: '本进程没有 profile 目录（未按 --profile 启动）' }
    }
    const markerPath = join(dir, '.qq-bot-owned.json')
    let marker: unknown
    try {
      marker = JSON.parse(readFileSync(markerPath, 'utf8'))
    } catch (error) {
      const why = error instanceof Error ? error.message : String(error)
      return { name, dir, home, ownedByBot: false, reason: `缺少或读不到归属标记 ${markerPath}：${why}` }
    }
    const record = typeof marker === 'object' && marker !== null ? marker as Record<string, unknown> : {}
    if (record['owner'] !== 'qq-life') {
      return { name, dir, home, ownedByBot: false, reason: `归属标记 owner 不是 qq-life：${String(record['owner'])}` }
    }
    const markedHome = record['home']
    if (typeof markedHome === 'string' && resolve(markedHome) !== home) {
      return { name, dir, home, ownedByBot: false, reason: `归属标记 home 与运行目录不一致：${markedHome} != ${home}` }
    }
    return { name, dir, home, ownedByBot: true }
  }

  /** Cheap liveness/version handshake for the QQ life bridge.
   * @param params - Optional bot protocol negotiation; omitted parameters preserve the current selection.
   * @returns loaded worker, profile and adapter identity.
   */
  qqLifeHello(params?: Record<string, unknown>): Record<string, unknown> {
    if (params?.capability_protocol !== undefined) this.roleCapabilityProtocol = params.capability_protocol === 1
    if (params?.media_adapter_protocol !== undefined) this.roleMediaAdapterProtocol = params.media_adapter_protocol === 1
    if (params?.consult_protocol !== undefined) this.roleConsultProtocol = params.consult_protocol === 1
    if (params?.experience_protocol !== undefined) this.roleExperienceProtocol = params.experience_protocol === 1
    if (params?.continuity_protocol === 1) {
      if (typeof params.continuity_enabled !== 'boolean') throw new TypeError('continuity_enabled must be boolean')
      this.roleContinuityEnabled = params.continuity_enabled
    }
    const identity = this.qqLifeIdentity()
    return {
      protocol: 'qq-life/1',
      relation_protocol: 2,
      resource_protocol: 1,
      spending_protocol: 1,
      memory_protocol: 1,
      runtime_context_policy: 'latest-only-v1',
      capability_protocol: this.roleCapabilityProtocol ? 1 : null,
      media_adapter_protocol: this.roleMediaAdapterProtocol ? 1 : null,
      consult_protocol: this.roleConsultProtocol ? 1 : null,
      experience_protocol: this.roleExperienceProtocol ? 1 : null,
      continuity_protocol: 1,
      continuity_enabled: this.roleContinuityEnabled,
      profile: identity.name,
      ...identity.dir === undefined ? {} : { profileDir: identity.dir },
      home: identity.home,
      profileOwnedByBot: identity.ownedByBot,
      roleAdapterVersion: QQ_ROLE_ADAPTER_VERSION,
      ...identity.reason === undefined ? {} : { profileOwnedReason: identity.reason },
      workerGeneration: process.pid, startedAt: this.qqLifeStartedAt,
      serverInfo: { name: 'deepseek-harness-sdk-runtime', version: '0.0.1' },
      capabilities: ['hello', 'tools/list', 'execute', 'status', 'cancel',
        'session/input', 'session/input_status', 'session/events', 'session/control', 'life/callbacks'],
      operations: this.qqLifeStore().all().length,
    }
  }

  /** List model-facing tool schemas visible to one session's agent.
   * @param params - execution scope and page cursor.
   * @returns schemas and next cursor.
   */
  async qqLifeToolsList(params: Record<string, unknown> | undefined): Promise<Record<string, unknown>> {
    if (!this.initialized) throw new Error('SDK server is not initialized')
    const sessionId = params?.executionScope
    if (typeof sessionId !== 'string' || sessionId.length === 0) {
      throw new TypeError('qq_life/tools/list requires a non-empty executionScope')
    }
    const limit = params?.limit === undefined ? 200 : params.limit
    if (typeof limit !== 'number' || !Number.isSafeInteger(limit) || limit <= 0) {
      throw new TypeError('qq_life/tools/list limit must be a positive safe integer')
    }
    const cursor = params?.cursor === undefined ? 0 : params.cursor
    if (typeof cursor !== 'number' || !Number.isSafeInteger(cursor) || cursor < 0) {
      throw new TypeError('qq_life/tools/list cursor must be a non-negative safe integer')
    }
    const rec = await this.getOrCreateSession(sessionId)
    this.assertLiveAgent(rec, sessionId)
    const schemas = this.ctx.tools.schemas(rec.handle.agent)
    const page = schemas.slice(cursor, cursor + limit)
    const next = cursor + page.length
    return { protocol: 'qq-life/1', tools: page, nextCursor: next < schemas.length ? next : null }
  }

  /**
   * Execute one caller-selected tool call inside the agent's own turn.
   * Replaying an operation id returns the recorded state instead of running the
   * tool again; a different request under the same id is refused.
   * @param params - identified operation and tool arguments.
   * @returns actual recorded operation state.
   */
  async qqLifeExecute(params: Record<string, unknown> | undefined): Promise<Record<string, unknown>> {
    if (!this.initialized) throw new Error('SDK server is not initialized')
    const operationId = params?.operationId
    if (typeof operationId !== 'string' || operationId.length === 0) {
      throw new TypeError('qq_life/execute requires a non-empty operationId')
    }
    const sessionId = params?.executionScope
    if (typeof sessionId !== 'string' || sessionId.length === 0) {
      throw new TypeError('qq_life/execute requires a non-empty executionScope')
    }
    const tool = params?.tool
    if (typeof tool !== 'string' || tool.length === 0) {
      throw new TypeError('qq_life/execute requires a non-empty tool name')
    }
    const args = params?.arguments ?? {}
    const hash = requestHash(tool, args)
    const store = this.qqLifeStore()
    const existing = store.get(operationId)
    if (existing !== undefined) {
      if (existing.requestHash !== hash) {
        throw new TypeError(`operation ${operationId} 已用于另一份请求（工具或参数不同），拒绝复用`)
      }
      return this.qqLifeSnapshot(existing)
    }
    const operation: QqLifeOperation = {
      operationId, sessionId, tool, arguments: args, requestHash: hash,
      status: 'prepared', startedAt: Date.now(),
    }
    store.put(operation)
    const deadlineMs = params?.deadlineMs
    if (deadlineMs !== undefined && (typeof deadlineMs !== 'number' || !Number.isFinite(deadlineMs) || deadlineMs <= 0)) {
      operation.status = 'failed_before_dispatch'
      operation.error = 'deadlineMs must be a positive finite number'
      operation.settledAt = Date.now()
      store.put(operation)
      return this.qqLifeSnapshot(operation)
    }
    let rec: SessionRecord
    try {
      rec = await this.getOrCreateSession(sessionId)
      this.assertLiveAgent(rec, sessionId)
    } catch (error: unknown) {
      operation.status = 'failed_before_dispatch'
      operation.error = error instanceof Error ? error.message : String(error)
      operation.settledAt = Date.now()
      store.put(operation)
      return this.qqLifeSnapshot(operation)
    }
    operation.status = 'dispatching'
    store.put(operation)
    let stopped = false
    const timer = deadlineMs === undefined ? undefined : setTimeout(() => {
      stopped = true
      rec.handle.agent.cancel({ kind: 'user' })
    }, deadlineMs)
    const task = (async (): Promise<void> => {
      try {
        const runExternalToolTurn = rec.handle.agent.runExternalToolTurn
        if (runExternalToolTurn === undefined) throw new Error('agent does not implement runExternalToolTurn')
        operation.result = await runExternalToolTurn.call(rec.handle.agent, {
          callId: operationId, name: tool, arguments: operation.arguments,
        })
        operation.status = 'result_recorded'
      } catch (error: unknown) {
        const message = error instanceof Error ? error.message : String(error)
        const cancelled = stopped || this.qqLifeCancelRequested.has(operationId)
        operation.status = cancelled ? 'cancelled' : 'unknown'
        operation.error = cancelled ? `已停止执行：${message}` : message
      } finally {
        if (timer !== undefined) clearTimeout(timer)
        operation.settledAt = Date.now()
        store.put(operation)
        this.qqLifeInflight.delete(operationId)
        this.qqLifeCancelRequested.delete(operationId)
      }
    })()
    this.qqLifeInflight.set(operationId, task)
    await task
    return this.qqLifeSnapshot(store.get(operationId) ?? operation)
  }

  /** Read one operation's settled or in-flight state straight from the ledger.
   * @param params - original operation identity.
   * @returns recorded state and settlement evidence.
   */
  qqLifeStatus(params: Record<string, unknown> | undefined): Record<string, unknown> {
    const operationId = params?.operationId
    if (typeof operationId !== 'string' || operationId.length === 0) {
      throw new TypeError('qq_life/status requires a non-empty operationId')
    }
    const operation = this.qqLifeStore().get(operationId)
    if (operation === undefined) throw new Error(`unknown qq_life operation: ${operationId}`)
    return this.qqLifeSnapshot(operation)
  }

  /**
   * Ask the owning agent to stop one operation, then report whether it really
   * stopped: only a settled operation is ever reported as cancelled.
   * @param params - original operation identity.
   * @returns cancellation request and actual settlement state.
   */
  async qqLifeCancel(params: Record<string, unknown> | undefined): Promise<Record<string, unknown>> {
    const operationId = params?.operationId
    if (typeof operationId !== 'string' || operationId.length === 0) {
      throw new TypeError('qq_life/cancel requires a non-empty operationId')
    }
    const waitMs = params?.waitMs === undefined ? 15000 : params.waitMs
    if (typeof waitMs !== 'number' || !Number.isSafeInteger(waitMs) || waitMs < 0) {
      throw new TypeError('qq_life/cancel waitMs must be a non-negative safe integer')
    }
    const store = this.qqLifeStore()
    const operation = store.get(operationId)
    if (operation === undefined) throw new Error(`unknown qq_life operation: ${operationId}`)
    if (isSettledStatus(operation.status)) return this.qqLifeSnapshot(operation)
    this.qqLifeCancelRequested.add(operationId)
    if (operation.status !== 'stopping') {
      operation.status = 'stopping'
      store.put(operation)
      this.sessions.get(operation.sessionId)?.handle.agent.cancel({ kind: 'user' })
    }
    const inflight = this.qqLifeInflight.get(operationId)
    if (inflight !== undefined) {
      await Promise.race([inflight, new Promise<void>(done => { setTimeout(done, waitMs).unref() })])
    }
    return this.qqLifeSnapshot(store.get(operationId) ?? operation)
  }

  /** One wire snapshot: what ran, how it ended, whether it is really over. */
  private qqLifeSnapshot(operation: QqLifeOperation): Record<string, unknown> {
    return {
      operationId: operation.operationId, executionScope: operation.sessionId,
      status: operation.status, settled: isSettledStatus(operation.status),
      tool: operation.tool, requestHash: operation.requestHash,
      workerGeneration: process.pid,
      ...operation.result === undefined ? {} : { result: operation.result },
      ...operation.error === undefined ? {} : { error: operation.error },
      startedAt: operation.startedAt,
      ...operation.settledAt === undefined ? {} : { settledAt: operation.settledAt },
    }
  }


  /**
   * Dispatch one incoming JSON-RPC request to its typed handler. Throws (→ a
   * JSON-RPC error response) on an unknown method.
   * @param method - the JSON-RPC method name.
   * @param params - the raw params object from the wire.
   * @returns the handler's result, to be serialized as the response.
   */
  async handleRequest(method: string, params: Record<string, unknown> | undefined): Promise<unknown> {
    if (method.startsWith('qq_life/session/')) {
      if (!this.initialized || this.shuttingDown) throw new Error('SDK runtime is not accepting input')
      this.roleSessions ??= new QqLifeSessions(this.ctx, this.transport, () => this.qqLifeHome(), async id => {
        const rec = await this.getOrCreateSession(id)
        this.assertLiveAgent(rec, id)
        return rec.handle.agent
      }, blocks => durablePromptContent(this.ctx, blocks), this.roleContinuityEnabled,
      { capability: this.roleCapabilityProtocol, media: this.roleMediaAdapterProtocol, consult: this.roleConsultProtocol, experience: this.roleExperienceProtocol })
      return this.roleSessions.handle(method.slice('qq_life/session/'.length), params ?? {})
    }
    switch (method) {
      case 'initialize':
        return this.initialize(params as unknown as InitializeParams)
      case 'session/prompt':
        return this.prompt(params as unknown as SessionPromptParams)
      case 'session/resume': {
        const sessionId = params?.sessionId
        if (typeof sessionId !== 'string' || sessionId.length === 0) {
          throw new TypeError('session/resume requires a non-empty sessionId string')
        }
        return this.resume({ sessionId })
      }
      case 'session/wait': {
        const sessionId = params?.sessionId
        if (typeof sessionId !== 'string') throw new TypeError('session/wait requires a sessionId string')
        return this.wait({ sessionId })
      }
      case 'session/working-directory/get':
      case 'session/working-directory/set': {
        if (typeof params?.sessionId !== 'string' || params.sessionId.length === 0) {
          throw new TypeError('working-directory requests require a non-empty sessionId')
        }
        if (method === 'session/working-directory/get') {
          return this.getWorkingDirectory({ sessionId: params.sessionId })
        }
        if (typeof params.path !== 'string') throw new TypeError('working-directory set requires a path string')
        return this.setWorkingDirectory({ sessionId: params.sessionId, path: params.path })
      }
      case 'qq_life/hello':
        return this.qqLifeHello(params)
      case 'qq_life/tools/list':
        return this.qqLifeToolsList(params)
      case 'qq_life/execute':
        return this.qqLifeExecute(params)
      case 'qq_life/status':
        return this.qqLifeStatus(params)
      case 'qq_life/cancel':
        return this.qqLifeCancel(params)
      case 'shutdown':
        return this.shutdown()
      default:
        throw new Error(`unknown DeepSeek Harness SDK runtime method: ${method}`)
    }
  }

  private async getOrCreateSession(sessionId: string): Promise<SessionRecord> {
    if (this.shuttingDown) throw new Error('SDK server is shutting down')
    const existing = this.sessions.get(sessionId)
    if (existing) return existing
    const pending = this.sessionCreations.get(sessionId)
    if (pending) return pending
    return this.openSession(sessionId, false)
  }

  private openSession(sessionId: string, resume: boolean): Promise<SessionRecord> {
    const creation = this.createSession(sessionId, resume).catch((error: unknown) => {
      if (resume || !(error instanceof Error) || !error.message.includes('already exists')) throw error
      return this.createSession(sessionId, true)
    })
    this.sessionCreations.set(sessionId, creation)
    void creation.then(
      () => { if (this.sessionCreations.get(sessionId) === creation) this.sessionCreations.delete(sessionId) },
      () => { if (this.sessionCreations.get(sessionId) === creation) this.sessionCreations.delete(sessionId) },
    )
    return creation
  }

  private async createSession(sessionId: string, resume: boolean): Promise<SessionRecord> {
    // No preset composition: this server's compositions keep the model-facing
    // rows in the host plane, so this agent reads them from the global layer. A
    // deployment that configures a roster has to join one here first
    // (@deepseek-ai/dsh-agent-preset-registry README, "Composing a child agent").
    const agentOptions = {
      provider: this.provider,
      model: this.model,
      ...this.reasoningEffort === undefined ? {} : { reasoningEffort: this.reasoningEffort },
      ...this.maxTokens === undefined ? {} : { maxTokens: this.maxTokens },
    }
    const handle = await (resume ? this.ctx.agents.resume({
      resumeSessionId: brandString<SessionId>(sessionId),
      agentOptions,
    }) : this.ctx.agents.create({
      sessionId: brandString<SessionId>(sessionId),
      meta: { cwd: this.cwd },
      agentOptions,
    }))
    const rec: SessionRecord = { handle, failure: undefined }
    this.sessions.set(sessionId, rec)
    return rec
  }

  private hasAdapterFor(provider: string): boolean {
    return this.ctx.get('llm')?.listProviders().some(entry => entry.id === provider) ?? false
  }
}
