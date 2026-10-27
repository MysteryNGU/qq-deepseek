/** Bot-owned Agent sessions, durable input admission and scoped life-tool callbacks. */
import type { Context } from '@deepseek-ai/cordis'
import type { Agent } from '@deepseek-ai/dsh-agent'
import { brandString } from '@deepseek-ai/dsh-brand'
import { createDeveloperMessage, freezeMessage, type ContentBlock, type MessageId, type UserMessage } from '@deepseek-ai/dsh-llm'
import { defineTool, assertObjectJsonSchema, validateJsonSchemaValue } from '@deepseek-ai/dsh-tools'
import type {} from '@deepseek-ai/dsh-system-prompt'
import type {} from '@deepseek-ai/dsh-session-query'
import type {} from '@deepseek-ai/dsh-token-meter'
import type {} from '@deepseek-ai/dsh-compaction'
import type { SkillCandidate, SkillDefinition } from '@deepseek-ai/dsh-skill'
import type { SessionEvent } from '@deepseek-ai/dsh-session'
import type { JsonRpcTransportPeer, SessionPromptParams } from '@deepseek-ai/dsh-sdk-protocol'
import { closeSync, existsSync, fsyncSync, mkdirSync, openSync, readFileSync, truncateSync, writeSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { createHash } from 'node:crypto'
import { AsyncLocalStorage } from 'node:async_hooks'
import { requestHash } from './qq-life-operations.ts'
import { QqLifeResources } from './qq-life-resources.ts'
import { QqLifeConsult, QQ_CONSULT_ADAPTER_VERSION } from './qq-life-consult.ts'
import { QqLifeContext, QQ_CONTEXT_ADAPTER_VERSION } from './qq-life-context.ts'

/** Fingerprint captured from the loaded adapter module, not reread after edits. */
export const QQ_ROLE_ADAPTER_VERSION = createHash('sha256').update(readFileSync(new URL(import.meta.url)))
  .update(QQ_CONTEXT_ADAPTER_VERSION)
  .update(QQ_CONSULT_ADAPTER_VERSION).digest('hex')

declare module '@deepseek-ai/dsh-tools' {
  interface ToolDefinition {
    /** Installed provider identity for Bot policy matching; never read from tool arguments. */
    readonly qqLifeIdentity?: { readonly package_id: string; readonly version: string;
      readonly policy?: { readonly effect: 'read' | 'write' | 'unknown'; readonly principal_adoption: boolean;
        readonly audience: 'owner' | 'public'; readonly account?: string | null; readonly resource?: string | null } }
  }
}

declare module '@deepseek-ai/dsh-llm' {
  interface MessageSourceMap {
    /** Bot-supplied input; only user_report originates in a human message. */
    'qq-life': { kind: 'qq-life'; inputId: string; origin: string }
    /** Persisted tool presentation state, revalidated against the scoped registry on resume.
     * @persistenceAttribution
     */
    'qq-life-tools': { kind: 'qq-life-tools'; scope: string; loaded: { name: string; hash: string }[] }
  }
}

interface Admission {
  sessionId: string
  inputId: string
  generation: number
  digest: string
  message: UserMessage
  mode: string
}

function record(value: unknown): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) throw new Error('invalid_request')
  return value as Record<string, unknown>
}

function text(value: unknown): string {
  if (typeof value !== 'string' || value.length === 0) throw new Error('invalid_request')
  return value
}

/** Expected wire failure with a stable recovery category. */
export class QqLifeSessionError extends Error {
  /**
   * @param code - recovery category.
   * @param detail - diagnostic. */
  constructor(readonly code: string, detail: string) { super(detail) }
}

/**
 * Owns one bot deployment's session inputs and registrations. Input identity is
 * durable before inbox admission; retries reconcile the same message identity.
 */
export class QqLifeSessions {
  private readonly toolCalls = new AsyncLocalStorage<{ sessionId: string; callId: string }>()
  private readonly executingCalls = new Set<string>()
  private readonly childCalls = new Map<string, { parentSessionId: string; callId: string }>()
  private readonly continuityTools = new Map<string, (extension: string, description?: string) => ReturnType<typeof defineTool>>()
  private readonly readModes = new WeakMap<Agent, 'internal' | 'audience'>()
  private readonly internalRegistrations = new Map<Agent, (() => void)[]>()
  private readonly continuityRegistrations = new Map<Agent, { enabled: boolean; disposers: (() => void)[] }>()
  private readonly methodProviders = new Map<Agent, { invalidate: () => void; dispose: () => void }>()
  private readonly admitted = new Map<string, Admission>()
  private readonly flights = new Map<string, Promise<Record<string, unknown>>>()
  private readonly bound = new Map<string, { generation: number; agent: Agent }>()
  private readonly context = new Map<string, string>()
  private readonly stableContext = new Map<string, string>()
  private resources: QqLifeResources | undefined
  private consult: QqLifeConsult | undefined
  private contextPolicy: QqLifeContext | undefined
  private readonly toolPresentation = new Map<Agent, { scope: string; version: string; loaded: Map<string, string>; disposers: (() => void)[] }>()
  private readonly requestPositions = new WeakMap<Agent, { turn: number; step: number }>()
  private readonly contextAgents = new Set<Agent>()
  private readonly opening = new Map<string, number>()
  private readonly disposers: (() => void)[] = []
  private loaded = false
  private enabled = false

  /**
   *
   * @param ctx - running profile.
   * @param transport - host callback transport.
   *
   * @param home - dedicated bot home.
   * @param agentFor - session creation/resume owner.
   */
  constructor(private readonly ctx: Context, private readonly transport: JsonRpcTransportPeer,
    private readonly home: () => string, private readonly agentFor: (id: string) => Promise<Agent>,
    private readonly prepareContent?: (blocks: SessionPromptParams['contentBlocks']) => Promise<ContentBlock[]>,
    private readonly initialContinuityEnabled = false,
    private readonly extensionProtocols: { capability?: boolean; media?: boolean; consult?: boolean; experience?: boolean; compact?: boolean } = {}) {}

  private load(): void {
    if (this.loaded) return
    const file = join(this.home(), 'qq-life', 'inputs.jsonl')
    if (existsSync(file)) {
      const body = readFileSync(file, 'utf8')
      const lines = body.split('\n')
      for (let index = 0; index < lines.length; index++) {
        const line = lines[index]!
        if (line.trim().length === 0) continue
        try {
          const item = record(JSON.parse(line))
          const value = item as Record<string, unknown> & Admission
          text(value.sessionId); text(value.inputId); text(value.digest)
          if (!Number.isSafeInteger(value.generation) || !value.message?.id) throw new Error('invalid admission')
          this.admitted.set(`${value.sessionId}\0${value.inputId}`, value)
        } catch (error: unknown) {
          if (index === lines.length - 1 && !body.endsWith('\n')) {
            truncateSync(file, Buffer.byteLength(body.slice(0, body.lastIndexOf('\n') + 1)))
            break
          }
          throw new QqLifeSessionError('history_gap', `Unreadable admission at line ${index + 1}: ${String(error)}`)
        }
      }
      // A complete final record can survive without its delimiter. Normalize
      // that delimiter before the next append rather than joining two records.
      if (!body.endsWith('\n') && readFileSync(file, 'utf8') === body) {
        const fd = openSync(file, 'a')
        try { writeSync(fd, '\n'); fsyncSync(fd) } finally { closeSync(fd) }
      }
    }
    this.loaded = true
  }

  private persist(value: Admission): void {
    const file = join(this.home(), 'qq-life', 'inputs.jsonl')
    mkdirSync(dirname(file), { recursive: true })
    const fd = openSync(file, 'a')
    try { writeSync(fd, `${JSON.stringify(value)}\n`); fsyncSync(fd) } finally { closeSync(fd) }
    this.admitted.set(`${value.sessionId}\0${value.inputId}`, value)
  }

  private async events(agent: Agent): Promise<readonly SessionEvent[]> {
    const query = this.ctx.get('sessionQuery')
    if (!query) throw new QqLifeSessionError('unavailable', 'Session query is required for reliable input recovery')
    using observation = await query.observeSession(agent.session.id, { projectionMode: 'none' })
    return observation.events
  }

  private owner(agent: Agent | undefined): { root: string; child: boolean } | undefined {
    if (!agent) return undefined
    const direct = this.bound.get(String(agent.session.id))
    if (direct?.agent === agent) return { root: String(agent.session.id), child: false }
    if (this.opening.has(String(agent.session.id)) && this.ctx.agents.get(agent.id) === agent) {
      return { root: String(agent.session.id), child: false }
    }
    const runtimeParent = this.ctx.agents.list().find(parent => this.ctx.agents.isOwnedBy(agent.session.id, parent))
    if (runtimeParent) {
      const owner = this.owner(runtimeParent)
      if (owner) return { root: owner.root, child: true }
    }
    let parent = agent.session.header.parentSession
    const visited = new Set<string>()
    while (parent && !visited.has(String(parent))) {
      const id = String(parent)
      visited.add(id)
      if (this.bound.has(id)) return { root: id, child: true }
      parent = this.ctx.sessions.get(parent)?.header.parentSession
    }
    return undefined
  }

  private async callback(agent: Agent, method: string, body: Record<string, unknown>): Promise<Record<string, unknown>> {
    const owner = this.owner(agent)
    if (!owner) throw new QqLifeSessionError('scope_mismatch', 'Agent is not bound to this bot')
    const reply = await this.transport.request(method, {
      session_id: String(agent.session.id), root_session_id: owner.root,
      direct_parent_session_id: agent.session.header.parentSession === undefined ? null : String(agent.session.header.parentSession),
      direct_parent_call_id: this.childCalls.get(String(agent.session.id))?.callId ?? null,
      caller: owner.child ? 'delegate' : 'role', ...body,
    })
    return record(reply)
  }

  private install(): void {
    if (this.enabled) return
    this.enabled = true
    this.resources = new QqLifeResources(this.ctx, this.home, agent => this.owner(agent),
      (agent, kind, record) => this.callback(agent, 'qq_life/life/read', { kind, record }))
    this.disposers.push(this.resources.observeCalls())
    this.contextPolicy = new QqLifeContext(this.ctx, agent => this.owner(agent) !== undefined,
      (agent, body) => this.callback(agent, 'qq_life/life/read', body), (agent,mode) => {
        const state=this.toolPresentation.get(agent)
        if (mode==='task' || !state || state.loaded.size===0) return
        this.persistToolPresentation(agent,state.scope,new Map())
        state.loaded.clear()
      })
    this.disposers.push(this.ctx.on('agent/request-ready', async (payload, next) => {
      await next()
      if (!this.owner(payload.agent) || !this.extensionProtocols.compact || this.readModes.get(payload.agent) === 'internal') return
      payload.signal.throwIfAborted()
      const shown = payload.agent.session.requestHeader()?.tools ?? []
      const current = this.ctx.tools.schemas(payload.agent)
      for (const tool of shown) {
        if (JSON.stringify(current.find(row => row.name === tool.name)) !== JSON.stringify(tool)) {
          throw new QqLifeSessionError('stale_context', 'Tool registry changed during request preparation')
        }
      }
      await this.contextPolicy!.prepare(payload)
      const finalSchemas=this.ctx.tools.schemas(payload.agent)
      for (const tool of shown) {
        if (JSON.stringify(finalSchemas.find(row => row.name===tool.name))!==JSON.stringify(tool)) {
          throw new QqLifeSessionError('stale_context','Tool registry changed before request freeze')
        }
      }
    }))
    if (this.extensionProtocols.consult) this.consult = new QqLifeConsult(this.ctx,
      (agent, body) => this.callback(agent, 'qq_life/life/commit', body))
    this.disposers.push(this.ctx.on('agent/created', async ({ agent }) => {
      if (this.owner(agent)) {
        const reply = await this.callback(agent, 'qq_life/life/read', { kind: 'static_context' })
        if (reply.ok === false) throw new QqLifeSessionError(String(reply.code), String(reply.message))
        this.configureReadMode(agent, reply)
        await this.configureContinuity(agent, this.initialContinuityEnabled)
      }
    }))
    this.disposers.push(this.ctx.on('agent/disposed', ({ agent }) => {
      for (const dispose of this.internalRegistrations.get(agent) ?? []) dispose()
      this.internalRegistrations.delete(agent)
      this.methodProviders.get(agent)?.dispose()
      this.methodProviders.delete(agent)
      for (const dispose of this.continuityRegistrations.get(agent)?.disposers ?? []) dispose()
      this.continuityRegistrations.delete(agent)
      this.contextAgents.delete(agent)
      this.context.delete(String(agent.session.id))
      this.stableContext.delete(String(agent.session.id))
      for (const dispose of this.toolPresentation.get(agent)?.disposers ?? []) dispose()
      this.toolPresentation.delete(agent)
    }))
    this.disposers.push(this.ctx.on('tools/execute', async (exec, next) => {
      if (!exec.agent || !this.owner(exec.agent)) return next()
      const call = { sessionId: String(exec.agent.session.id), callId: String(exec.callId) }
      const key = `${call.sessionId}\0${call.callId}`
      this.executingCalls.add(key)
      this.transport.notify('qq_life/tool.started', { sessionId: call.sessionId, callId: call.callId })
      try { return await this.toolCalls.run(call, next) } finally { this.executingCalls.delete(key) }
    }))
    this.disposers.push(this.ctx.on('agent/pre-step', async (payload, next) => {
      if (this.owner(payload.agent)) this.methodProviders.get(payload.agent)?.invalidate()
      const decision = await next()
      if (this.owner(payload.agent) && decision.kind === 'enter') {
        const refs = decision.messages.flatMap(message => message.source?.kind === 'qq-life' ? [message.source.inputId] : [])
        this.resources!.admitted(payload.agent, payload.turn, refs)
      }
      return decision
    }))
    this.disposers.push(this.ctx.on('tools/post-execute', async (exec, result, next) =>
      this.resources!.post(exec, result, await next())))
    this.disposers.push(this.ctx.on('tools/ptc-dispatch-log', async (dispatch, next) =>
      this.resources!.present({ ...dispatch.exec, name: dispatch.name, callId: dispatch.subCallId }, await next(), dispatch.isError)))
    this.disposers.push(this.ctx.on('system-prompt/assemble', async (assembly, context, next) => {
      const agent = context.agent
      if (agent && this.owner(agent)) {
        const reply = await this.callback(agent, 'qq_life/life/read', { kind: 'static_context' })
        if (reply.ok === false) throw new QqLifeSessionError(String(reply.code), String(reply.message))
        this.configureReadMode(agent, reply)
        await this.configureContinuity(agent, reply.continuity_protocol === 1 && reply.continuity_enabled === true)
        await this.configureContracts(agent, reply)
        const text = String(reply.stable_text ?? '')
        this.stableContext.set(String(agent.session.id), text)
        const section = assembly.sections.find(value => value.name === 'qq-role-rules')
        if (section) section.text = text
      }
      const result = await next()
      if (agent && this.owner(agent) && this.extensionProtocols.compact && this.readModes.get(agent) !== 'internal') {
        const state = this.toolPresentation.get(agent)
        if (!state) throw new QqLifeSessionError('protocol_mismatch', 'Host tool contracts were not supplied')
        const base = new Set(['life_read','life_say','life_rest','life_remember','life_intent','life_appraise'])
        if (this.owner(agent)?.child) {
          base.clear();base.add('life_read')
        }
        const schemas = this.ctx.tools.schemas(agent)
        for (const [name, hash] of state.loaded) {
          if (this.toolIdentity(agent, name).schema_hash !== hash) state.loaded.delete(name)
        }
        result.tools = schemas.filter(tool => base.has(tool.name) || state.loaded.has(tool.name))
      }
      return result
    }))
    this.disposers.push(this.ctx.on('agent/request', async (payload, next) => {
      if (this.owner(payload.agent)) {
        this.requestPositions.set(payload.agent,{ turn: payload.turn,step: payload.step })
        this.installContext(payload.agent)
        const reply = await this.callback(payload.agent, 'qq_life/life/read', {
          kind: 'context', turn: payload.turn, step: payload.step,
        })
        if (reply.ok === false) throw new QqLifeSessionError(String(reply.code), String(reply.message))
        this.configureReadMode(payload.agent, reply)
        payload.signal.throwIfAborted()
        // Retire the previous snapshot only after its replacement was obtained.
        // The loop then commits the freshly rendered aggregate before streaming.
        this.retireRuntimeContexts(payload.agent, payload.turn, payload.step)
        this.context.set(String(payload.agent.session.id), String(reply.text ?? ''))
        this.stableContext.set(String(payload.agent.session.id), String(reply.stable_text ?? ''))
        this.resources!.configure(payload.agent, reply.resource_policy, { turn: payload.turn, step: payload.step },
          reply.context_plan && typeof reply.context_plan === 'object' && 'mode' in reply.context_plan ? reply.context_plan.mode : undefined)
        if (this.extensionProtocols.compact && this.readModes.get(payload.agent) !== 'internal') this.contextPolicy!.configure(payload.agent, reply)
        await this.configureContinuity(payload.agent, reply.continuity_protocol === 1 && reply.continuity_enabled === true)
      }
      return next()
    }))
    this.disposers.push(this.ctx.on('tools/pre-execute', async (exec, next) => {
      if (!exec.agent || !this.owner(exec.agent)) return next()
      if (exec.name === 'life_say' || exec.name === 'life_rest') {
        const events = await this.events(exec.agent)
        const last = events.findLast(event => event.type === 'assistant/message')
        if (last?.type === 'assistant/message') {
          const calls = last.data.message.content.filter(block => block.type === 'tool-call')
          if (calls.some(call => call.type === 'tool-call' && call.id !== exec.callId)) {
            return { kind: 'deny', reason: 'Submit the final utterance or rest alone after other tools have settled.' }
          }
        }
      }
      const reply = await this.callback(exec.agent, 'qq_life/life/commit', {
        kind: 'before_tool', call_id: String(exec.callId), tool: exec.name, arguments: exec.arguments,
        tool_identity: this.toolIdentity(exec.agent, exec.name),
      })
      if (reply.ok === false) return { kind: 'deny', reason: JSON.stringify(reply) }
      return next()
    }))
    for (const [name, description] of [
      ...this.extensionProtocols.experience ? [['life.simulation', 'Adult intimate experience only, experience_protocol:1. Read life_read(proposal:{kind:simulation}) first. Every proposal needs experience_domain:adult_intimate,base_revision from state_revision,sources:[current actual user event ids],subject_actor_id. action:enter needs adult_participants:true,context_confirmed:true,willingness:interested and current explicit adult intimate context; ordinary companionship, work and exploration never enter. action:appraise_input evaluates ONE current language input independently of continuous mode: direction:increase|decrease|unchanged,effects:{arousal?:-0.25..0.25,valence?:-0.25..0.25,engagement?:-0.25..0.25},confidence:0..1,reason. Meaning determines increase, decrease or no effect; never add points per sentence. Missing evaluation remains pending. action:start|adjust needs profile_confirmed:true,input_profile:{method,intensity:0..1,valence:-1..1,rhythm:continuous|intermittent,on_seconds?,off_seconds?}; clarify an unspecified method first. adjust|pause|resume needs run_ref; resume requires current willingness. stop disables continuous input while keeping the scene; exit closes the scene and disables continuous input. Silence, 45 minutes, turn end and a peak do not stop it. A stopped run needs a new explicit start; never replay old commands. A peak returns persistent transition_refs, before/after/actual_delta and state_revision; read/confirm the result BEFORE composing any reaction. No fixed lines or numerical announcements. action:ack with transition_refs marks understood results. Revision uses a new call id and supersedes_call_key from the original receipt, replaying the original time without resending expression. Hourly review reads this domain, never adds its fast effects again. Self output, imagination and authorized simulation are not evidence that another person acted or consented. Replies reflecting a simulation run carry experience_run_ref and experience_control_revision from the confirmed receipt.']] : [],
      ...this.extensionProtocols.consult ? [['life.consult', 'Consult Codex asynchronously. proposal.action:ask|status|read|cancel|close. ask:{message,topic_id?,request_id?,intent_ref?,sources?,handled_input_ids?}. Reuse topic_id for follow-ups; reuse request_id only to check/retry the SAME request. A queued job is not a completed answer. status/read/cancel take request_id; read supports cursor/limit_bytes. close takes topic_id and never kills running work. A durable consult_result returns the actual outcome. Continue unrelated conversation while waiting; unknown is not success and must never be replayed. Ordinary chat does not require creating a long-term task.']] : [],
      ['life.media', 'Submit media without waiting for generation. proposal.action:submit|status|cancel. submit: {kind:image,prompt,width?,height?,reference_asset_ids?,seed?,sources?,intent_ref?}, or {kind:speech,text,voice_id?,voice_revision?,delivery?,segments?:[{text,pause_after_ms?,delivery?}],sources?,intent_ref?}. Supported parameters and references depend on the configured backend. The scoped default voice is frozen when omitted. submit returns job_id immediately; status/cancel take job_id. Actual completion/failure is a media_result input. The original role chooses inspection, revision or sharing through life_say image/audio assets. Generated files are not automatically shared; a path or metadata is not visual inspection. Do not repeatedly poll a model for progress.'],
      ['life.read', 'Read current materials. proposal.kind: simulation (current adult_intimate state, controls, input coverage and persistent transition receipts; after_transition_seq?), experience_review (sources; read-only fast-field coverage for hourly review), recall (query,mode:auto|text|semantic|related,ref?,cursor?,limit?), life_source (ref,cursor?,max_tokens?), memory_index, self, relationship (actor_id), relationship_semantics, self (persona_audience:private|public selects a persona file and returns persona_path/persona_hash; private is owner-private-chat only), intents (default status:open; status:all includes finished records; ref reads one full task; limit/cursor paginate whole rows), reminders, appraisals, self_changes, skill (omit name for directory), usage (since?,until?,intent_ref?,session_id?,root_session_id?,turn?,global?:true only for owner). Usage is provider-reported; unknown is not free, estimates are separate and reasoning is not added twice. observation takes event_id or a visible result_ref, mode:summary|search|lines, query?,start_line?,offset_chars?,limit?,cursor?,before_lines?,after_lines?,limit_matches?,max_tokens?. Reads are bounded and return exact line positions, hash, coverage and next_cursor. Partial results are readable: follow next_read or next_cursor before claiming a complete list. Task lists must be read through the final page before replying; shortened task details provide their own next_read. Old include_full:true starts bounded pagination, never promises the whole result. message (platform message_id), voices (discover?:true,language?:zh), media (asset_id,perception?:transcript|frame_set,timestamps?:seconds[]; action:import with path for an actual local image or 16bit PCM WAV, media_kind:image|audio). Media observations retain source and coverage; a local path alone is not perception.'],
      ['life.remember', 'Save {text,kind:experience|preference|reminder|inference|imagination|roleplay,sources:event_ids[],cues?:[],always_load?:true}. Preserve actual meaning and sources. A self thought is not an external fact. For durable relationship material use kind:relationship_note, actor_id (actual QQ account), operation:create|replace|resolve|revoke, relation_kind:preference|agreement|repair|expectation|role_description, origin:explicit_statement|interpretation, text,sources,confidence?,salience:low|normal|high,lifetime_days?. replace/resolve/revoke require the current base_note_ref. Resolve only repairs/agreements. A resolved or superseded note never becomes active by repeating its call. Ordinary task promises belong in life.intent.'],
      ['life.intent', 'Save {goal,kind:once|ongoing,origin:user|self,checkpoint,wake_after_seconds?,sources?}. Existing intentions require ref and base_revision; action:save|resolve|wait|pause|cancel|resume|done|merge. merge requires ref/base_revision and target_ref/target_base_revision in this conversation; it retires the duplicate and retains the target progress. Read existing tasks first, reuse stable topic_key and do not abandon a task merely because it is old. wait saves question and optional actor_id for this intention; it pauses dependent execution without blocking unrelated life. resume from a reply wait requires newly consumed handled_input_ids from that actual person; silence, wakes and timeouts never count as consent. action:resolve with handled_input_ids handles ordinary chat without creating a task. Long directions need end_direction:true to finish. Include activity_changes:[{intent_ref,base_revision,decision:continue|revise|pause|cancel|unrelated,patch?}] when handling new input.'],
      ['life.appraise', 'Evaluate actual semantic meaning; no keyword scoring. {sources:event_ids[],interpretation,meaning_kind,subject_actor_id?,affect_impulse:{joy,concern,frustration,hurt,anticipation},certainty,recovery_half_life_seconds?:{emotion:seconds},persistence_reason?,drive_evidence?,context_refs?,based_on_state_version?}. Relationship changes require relation_protocol:2, actual QQ subject_actor_id, based_on_relationship_version from life.read(kind:relationship,actor_id), relation_impacts:[{metric:familiarity|trust|comfort|bond,direction:increase|decrease|unchanged,magnitude:slight|meaningful|major,confidence:0..1}]. Read results and current context include four metrics, meanings, basis, visible evidence, policy and relationship_version. Owner metrics stay in [0.7,1], other people in [0,0.9]. bond is lasting personal attachment, not pleasure or sexual drive. No absolute scores, delta, relation_evidence or relationship_evidence; old fields return protocol_mismatch. Optional relation_context_changes use the relationship_note operations from life.remember in the same atomic appraisal. One actual stimulus settles once; duplicate returns its receipt. stimulus_overlap requires an explicit revision: supersedes_appraisal_id and revision+1. On stale_context reread the versions before deciding. Self imagination may use self_text but cannot prove another person acted.'],
      ['life.change_self', 'Actually save and activate: target:persona with content/base_hash updates the independent private persona source; target:persona_public with content/base_hash updates only the independent public persona source. Select the source with life_read(kind:self,persona_audience:private|public) and use that file’s persona_hash; public conversations cannot read the private source. Neither source is generated from the other; do not submit legacy public/private section markers; target:skill with name/content; target:dynamics with patch:{drives:{dimension:{parameter:value}},affect:{half_life:{emotion:seconds}}}; target:code with patch:{file,old,new} or patch:{changes:[{file,old,new}|{file,action:create,content}],migrations?:[{id,sql:[]}]}, plus independent probes:[{module,function,arguments,expected,critical}]. Multi-file/new-module/core changes use immutable candidates; migrations only add tables, columns or indexes. Read the publication receipt: rejected or applied:false is not an active upgrade. target:dsh with edits:[{path,old,new}],reason,verification_goal queues checked native DSH source/build/worker activation after the current turn rests; read self_changes for the actual outcome and then verify the original goal. target:worker with verification_goal queues a restart after native plugin_manager installation when its receipt requires restart; target:plugin action:remove with bundle and verification_goal removes via the stopped-profile CLI and reopens the worker. Voice target:voice supports action:register with voice:{id,engine:edge-tts,engine_voice,languages?,parameters?}, or a configured background voice:{id,name?,engine,provider_id?,reference_asset_id,revision?,parameters?} and sample_text?. Local registration queues real background synthesis and returns job_id; choose a default only after its verified result. action:default takes voice_id,global?:bool; private local references cannot become a global public default. Edge retains its preset compatibility; background voices use scoped references, adapter-owned parameters and frozen revisions. The saved default keeps its selected voice revision until explicitly changed. Runtime code activation is distinct from saved code.'],
      ['life.say', 'Send a natural expression to the current conversation. {text,handled_input_ids,activity_changes?,appraisal?}, or parts:[{kind:text,text,mode?:auto|single|artifact},{kind:image,asset_id},{kind:audio,asset_id},{kind:voice,text,voice_id,parameters?}]. Pure images/voice are allowed; you choose the medium. New call IDs do not create a new reply slot. Resolve unknown delivery first; new_message:true explicitly starts another message after the previous one is confirmed. Sending normally continues the current activity: you may read, act or send another distinct message without a new wake or user reply. Decide when to stop with life_rest or end_turn:true on your last expression. A successful reply reports turn_complete; only true ends the activity. purpose:notice is an optional heads-up and also continues. No compulsory pre-action report or fixed message count. When promising to wait for an answer, include wait_for_reply:{intent_ref,base_revision,actor_id?}; the hold is saved before dispatch. It lasts until you understand a new answer and resume that intention; this question ends the current turn. Submit alone after other tool results have settled.'],
      ['life.rest', 'Keep your thoughts private and finish this turn. Include handled_input_ids if relevant. reflection_outcome:unchanged|new_thought|action|unspecified and next_attention:{mode:adaptive|after|event|none,after_seconds?,intent_ref?,reason?}. Adaptive uses the configured random idle interval; a thought need not be productive. after is an explicit time, including short task checks; event needs an existing intent_ref and never treats silence as consent. none stops this idle plan, not incoming messages. wake_after_seconds is a legacy after adapter and must not be combined with next_attention. Sending a message does not schedule sleep. Only rest, end_turn:true or a reply-wait question ends the current activity; existing explicit plans survive that end.'],
    ]) {
      const create = (extension = '', replacementDescription?: string) => defineTool({
        name: name!.replace('.', '_'), description: replacementDescription ?? description! + extension,
        parameters: { proposal: { type: 'json', required: true, description:
          'Structured proposal. Say: {text,handled_input_ids,activity_changes?,appraisal?}; rest: {thought?,wake_after_seconds?,handled_input_ids}; other tools: {action?,ref?,query?,text?,kind?,sources?,patch?}.' } },
        output: { schema: { type: 'json' }, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] },
        isConcurrencySafe: () => false,
        execute: async (args, exec) => {
          if (!exec.agent) throw new Error('life tools require a bound Agent')
          const reply = name === 'life.consult' && this.consult ? await this.consult.execute(exec.agent, args.proposal, exec) : await this.callback(exec.agent, name === 'life.read' ? 'qq_life/life/read' : 'qq_life/life/commit', {
            kind: name, call_id: String(exec.callId), proposal: args.proposal,
            tool_identity: this.toolIdentity(exec.agent,name!.replace('.','_')),
          })
          if (reply.ok === false) throw new QqLifeSessionError(String(reply.code), JSON.stringify(reply))
          if (name === 'life.read' && reply.tool_load !== undefined) this.stageTool(exec.agent, reply.tool_load)
          if (name === 'life.rest' || name === 'life.say' && reply.turn_complete === true
            || this.readModes.get(exec.agent) === 'internal' && reply.turn_complete === true) exec.concludeTurn()
          return JSON.parse(JSON.stringify(reply))
        },
      })
      this.disposers.push(this.ctx.tools.register(create()))
      this.continuityTools.set(name!, create)
    }
  }

  private async configureContracts(agent: Agent, reply: Record<string, unknown>): Promise<void> {
    if (!this.extensionProtocols.compact || this.readModes.get(agent) === 'internal') return
    const catalog = record(reply.tool_contracts), version = text(catalog.version), scope = `${text(reply.presentation_scope)}:${agent.session.id}`
    const old = this.toolPresentation.get(agent)
    if (old?.scope === scope && old.version === version) return
    if (!Array.isArray(catalog.items)) throw new QqLifeSessionError('protocol_mismatch', 'Incomplete host tool catalog')
    const definitions = catalog.items.flatMap(value => {
      const item = record(value), name = text(item.name), parameters = item.parameters
      assertObjectJsonSchema(parameters)
      const original = this.continuityTools.get(name.replace('_','.'))?.('')
      if (!original && (name === 'life_simulation' && !this.extensionProtocols.experience || name === 'life_consult' && !this.extensionProtocols.consult)) return []
      if (!original) throw new QqLifeSessionError('protocol_mismatch', `Unknown host tool ${name}`)
      return [{ ...original, name, description: text(item.description), parameters: { ...parameters },
        execute: async (args: unknown, exec: Parameters<typeof original.execute>[1]) => {
          const errors = validateJsonSchemaValue(parameters, args)
          if (errors.length) throw new QqLifeSessionError('invalid_input', errors.join('; '))
          return original.execute(args, exec)
        } }]
    })
    const loaded = old?.scope === scope ? new Map(old.loaded) : new Map<string, string>()
    if (!old) {
      const events = await this.events(agent)
      const saved = events.findLast(event => event.type === 'developer/message' && event.data.message.source.kind === 'qq-life-tools')
      if (saved?.type === 'developer/message' && saved.data.message.source.kind === 'qq-life-tools'
        && saved.data.message.source.scope === scope) {
        for (const item of saved.data.message.source.loaded) loaded.set(item.name, item.hash)
      }
    }
    // All schemas are validated before any replacement becomes visible.
    for (const dispose of old?.disposers ?? []) dispose()
    const disposers: (() => void)[] = []
    try {
      for (const definition of definitions) disposers.push(agent.ctx.tools.register(definition))
    } catch (error) {
      for (const dispose of disposers) dispose()
      this.toolPresentation.delete(agent)
      throw error
    }
    this.toolPresentation.set(agent, { scope, version, loaded, disposers })
  }

  private stageTool(agent: Agent, raw: unknown): void {
    const load = record(raw), state = this.toolPresentation.get(agent)
    const name = text(load.name), hash = text(load.schema_hash)
    if (!state || load.status !== 'ready' || `${String(load.presentation_scope)}:${agent.session.id}` !== state.scope
      || !this.ctx.tools.schemas(agent).some(tool => tool.name === name)
      || this.toolIdentity(agent, name).schema_hash !== hash) {
      throw new QqLifeSessionError('stale_context', 'Tool load failed current scope/hash validation')
    }
    if (state.loaded.get(name) === hash) return
    const loaded = new Map(state.loaded); loaded.set(name,hash)
    this.persistToolPresentation(agent,state.scope,loaded)
    state.loaded.set(name,hash)
  }

  private persistToolPresentation(agent: Agent, scope: string, loaded: Map<string,string>): void {
    const position = this.requestPositions.get(agent)
    if (!position) throw new QqLifeSessionError('stale_context','Tool load has no current request position')
    agent.session.append('developer/message', { ...position,
      message: createDeveloperMessage({ content: [], source: { kind: 'qq-life-tools', scope,
        loaded: [...loaded].map(([name, hash]) => ({ name, hash })) } }),
    }, { surfaceOp: 'append' })
  }

  /**
   * Capture the exact delegating call while the child is being created.
   *
   * @param parentSessionId - actual Session parent.
   * @param childSessionId - created child.
   *
   * @returns the causal call id, or undefined for non-tool creation.
   */
  captureChildCall(parentSessionId: string, childSessionId: string): string | undefined {
    const call = this.toolCalls.getStore()
    if (call?.sessionId !== parentSessionId) return undefined
    this.childCalls.set(childSessionId, { parentSessionId, callId: call.callId })
    return call.callId
  }

  /** Check whether this adapter recorded the child's causal call.
   * @param childSessionId - actual child.
   * @returns whether this bot recorded its creating or resuming call. */
  ownsChildCall(childSessionId: string): boolean { return this.childCalls.has(childSessionId) }

  private targetsForCalls(root: string, value: unknown): { targets: Set<string>; descendants: Set<string> } {
    if (value === undefined) return { targets: new Set(), descendants: new Set() }
    if (!Array.isArray(value) || value.length > 100) throw new QqLifeSessionError('invalid_input', 'parent_calls must be a bounded list')
    const targets = new Set<string>()
    const descendants = new Set<string>()
    for (const item of value) {
      const call = record(item), session = text(call.session_id), id = text(call.call_id)
      const agent = this.ctx.agents.list().find(candidate => String(candidate.session.id) === session)
      if (!agent || this.owner(agent)?.root !== root) throw new QqLifeSessionError('scope_mismatch', 'Cancel call does not belong to the bound root')
      if (this.executingCalls.has(`${session}\0${id}`)) targets.add(session)
      for (const [child, parent] of this.childCalls) {
        if (parent.parentSessionId === session && parent.callId === id) { targets.add(child); descendants.add(child) }
      }
    }
    return { targets, descendants }
  }

  private async configureContinuity(agent: Agent, enabled: boolean): Promise<void> {
    if (this.readModes.get(agent) === 'internal') return
    if (this.extensionProtocols.compact) {
      if (this.extensionProtocols.capability) this.installMethods(agent)
      return
    }
    const registrations = this.continuityRegistrations.get(agent)
    if (registrations?.enabled !== enabled) {
      for (const dispose of registrations?.disposers ?? []) dispose()
      const disposers: (() => void)[] = []
      this.continuityRegistrations.set(agent, { enabled, disposers })
      for (const [name, create] of this.continuityTools) {
        const extension = (enabled ? this.continuityDescription(name) : '') + this.extensionDescription(name)
        if (extension) disposers.push(agent.ctx.tools.register(create(extension)))
      }
    }
    if (this.extensionProtocols.capability) this.installMethods(agent)
  }

  private configureReadMode(agent: Agent, reply: Record<string, unknown>): void {
    const mode = reply.processing_mode === 'internal' ? 'internal' : 'audience'
    const previous = this.readModes.get(agent)
    if (previous !== undefined && previous !== mode) {
      throw new QqLifeSessionError('stale_context', 'Read purpose changed; create a new Session')
    }
    this.readModes.set(agent, mode)
    if (mode !== 'internal' || this.internalRegistrations.has(agent)) return
    for (const dispose of this.continuityRegistrations.get(agent)?.disposers ?? []) dispose()
    this.continuityRegistrations.delete(agent)
    this.methodProviders.get(agent)?.dispose()
    this.methodProviders.delete(agent)
    const names = new Set(['life_read', 'life_remember', 'life_rest'])
    const disposers: (() => void)[] = []
    this.internalRegistrations.set(agent, disposers)
    disposers.push(agent.ctx.tools.restrict({ allow: [...names] }))
    disposers.push(agent.ctx.tools.guard(exec => names.has(exec.name) ? undefined : 'Internal memory work cannot use outgoing or execution tools.'))
    for (const [name, description] of [
      ['life.read', 'Read the assigned original evidence with proposal.kind:memory_batch,cursor?,limit?. Follow every page before submitting complete coverage.'],
      ['life.remember', 'Submit the assigned evidence-backed summary with proposal.kind:organized_memory,coverage_digest,base_revision,claims:[{text,kind:observed|reported|interpreted|hypothetical,evidence_refs:[]}],omitted_refs?,unresolved_refs?. Optional ordinary_fragments:[{source_ref,classification:ordinary,category:recommendation|everyday_event|public_finding,uncertain:false,spans:[[byte_start,byte_end]]}] proposes exact ordinary quotes, excluding secrets, credentials and sensitive personal details. Byte ranges reference complete UTF-8 source text. This never authorizes account actions or changes persona.'],
      ['life.rest', 'Finish internal work after a successful summary commit, or pause it with proposal.reason when blocked. No message is sent.'],
    ]) {
      const create = this.continuityTools.get(name!)!
      disposers.push(agent.ctx.tools.register(create('', description)))
    }
  }

  private extensionDescription(name: string): string {
    if (name === 'life.read' && this.extensionProtocols.capability) return ' capability_protocol:1. kind:capabilities, category?:tools|methods|media, query?,ref?,cursor?,limit?:1..20 (default 8). Browse only installed capabilities of this Agent; an empty result says nothing about external marketplaces. A ref expands schema and limits; large schema pages use field:schema,offset. Cursors require the original version and filters. kind:skill supports name or ref and returns the actual hash/version/visibility. Methods are also available through the native skill tool. Registry presence, readiness, permitted use and goal verification are separate facts.'
    if (name === 'life.change_self' && this.extensionProtocols.capability) return ' capability_protocol:1. target:skill updates require the read base_hash; create requires create:true,base_hash:null. Optional metadata:{description,visibility:private|public,sources,capability_refs,public_content?}; public content must be a separate explicitly public generic method without private sources. Stale hashes must be reread and merged. target:worker with component:media,verification_goal reloads installed media declarations from the current configuration at the existing idle change point, retaining frozen jobs and defaults; component:dsh is the existing SDK restart. Applied files or a loaded declaration do not prove behavioral improvement. Inspect the new capabilities and test the original question.'
    if (name === 'life.media' && this.extensionProtocols.media) return ' media_adapter_protocol:1. Optional provider_id and provider_options select an enabled backend explicitly. Read kind:capabilities,category:media or kind:media_capabilities for the actual schema, model revision, reference/mask support and output. Private options cannot override public fields, scope, resource limits or execution paths. Defaults freeze on first submission; retries reuse them. Unsupported fields fail explicitly. Output generation, real visual inspection and delivery remain separate.'
    return ''
  }

  private toolIdentity(agent: Agent, name: string): Record<string, unknown> {
    const schema = this.ctx.tools.schemas(agent).find(tool => tool.name === name)
    const identity = this.ctx.tools.get(name, agent)?.qqLifeIdentity
    return { package_id: identity?.package_id ?? null, version: identity?.version ?? null, policy: identity?.policy ?? null,
      schema_hash: createHash('sha256').update(JSON.stringify(schema ?? null)).digest('hex') }
  }

  private installMethods(agent: Agent): void {
    if (this.methodProviders.has(agent)) return
    const skills = agent.ctx.get('skills')
    if (skills === undefined) return
    let invalidate = (): void => undefined
    const dispose = skills.registerProvider(control => {
      invalidate = control.invalidate
      return {
        name: `qq-life-${String(agent.session.id)}`,
        list: async (options): Promise<SkillCandidate[]> => {
          control.signal.throwIfAborted(); options.signal?.throwIfAborted()
          const reply = await this.callback(agent, 'qq_life/life/read', { kind: 'method_catalog' })
          control.signal.throwIfAborted(); options.signal?.throwIfAborted()
          if (reply.ok === false) throw new QqLifeSessionError(String(reply.code), String(reply.message))
          if (!Array.isArray(reply.items)) throw new QqLifeSessionError('invalid_input', 'Method catalog requires items')
          return reply.items.map(value => {
            const item = record(value)
            return { name: text(item.name), description: text(item.summary), provider: `qq-life-${String(agent.session.id)}`,
              source: 'custom', rank: 600, invocation: { modelInvocable: true, userInvocable: true },
              locator: { ref: text(item.ref), hash: text(item.hash) }, metadata: { hash: item.hash, version: item.revision },
              resourceBase: { kind: 'opaque', description: 'Local Bot method; read associated materials only within this conversation.' } }
          })
        },
        get: async (candidate, options): Promise<SkillDefinition> => {
          control.signal.throwIfAborted(); options.signal?.throwIfAborted()
          const locator = record(candidate.locator)
          const reply = await this.callback(agent, 'qq_life/life/read', { kind: 'method_get', ref: text(locator.ref), hash: text(locator.hash) })
          control.signal.throwIfAborted(); options.signal?.throwIfAborted()
          if (reply.ok === false) { control.invalidate(); throw new QqLifeSessionError(String(reply.code), String(reply.message)) }
          if (reply.hash !== locator.hash) { control.invalidate(); throw new QqLifeSessionError('stale_context', 'Method changed; read its current catalog') }
          return { ...candidate, content: text(reply.text) }
        },
      }
    })
    const entry = { invalidate: (): void => { invalidate() }, dispose }
    this.methodProviders.set(agent, entry)
  }

  private invalidateMethods(root: string): void {
    for (const [agent, provider] of this.methodProviders) if (this.owner(agent)?.root === root) provider.invalidate()
  }

  private continuityDescription(name: string): string {
    const descriptions: Record<string, string> = {
      'life.read': ' continuity_protocol:1. kind:continuity, view:overview|thoughts|activities|reflection_batch, query?,ref?,cursor?,limit?:1..20. Activity result_available is a tool result, never goal completion; coverage and actual delivery are separate. reflection_batch offers an identified contiguous increment; read-only access never advances its cursor. overview includes scoped retry_guards; global resource contention reveals no other audience details.',
      'life.remember': ' continuity_protocol:1. kind:inference with continuity:{type:thought|reflection,action:create|replace|archive,base_ref?,base_version?,batch_ref?,reviewed_ids?,outcome?:summarized|no_new_insight}. text is at most 400 characters; cues at most 8 strings of 40 characters. Replace/archive require the current note version. New reflections require an issued reflection_batch and every offered reviewed_id; summarized needs actual batch sources, no_new_insight omits text. Corrections to older reflections can replace without advancing a batch. Archive changes no reflection cursor. These are sourced interpretations, never facts or repeated appraisals.',
      'life.intent': ' continuity_protocol:1. ongoing may include interest:{cues,open_question}; bind:false is allowed only on action:save and stores an interest without changing the current execution binding. Active interests do not occupy a work slot. Optional retry_probe:{guard_ref,base_version,change_note,changed_source_refs,candidate:{tool,arguments}} declares one materially different next attempt, never dispatches it or resumes a user hold. The host freezes origin from actual inputs and ancestry; changing origin, call ids or checkpoint text cannot bypass resource or retry limits.',
      'life.rest': ' continuity_protocol:1. keep_thought:true with thought (1..400 characters) and thought_cues?:up to 8 strings saves a searchable private interpretation atomically with successful rest and returns note_ref/version. Default false preserves only the existing thought event. No extra task or model loop is created.',
      'life.say': ' continuity_protocol:1. Optional continuity_refs:up to 5 scoped thought/activity references associate this expression with its materials. Actual confirmed/partial/unknown delivery comes from original delivery receipts; association never archives a thought or completes an intention.',
    }
    return descriptions[name] ?? ''
  }

  private async bind(id: string, generation: number): Promise<Agent> {
    this.install()
    const existing = this.bound.get(id)
    if (existing && existing.generation !== generation) throw new QqLifeSessionError('stale_context', 'Session generation changed')
    this.opening.set(id,generation)
    let agent: Agent
    try { agent = await this.agentFor(id) } finally { this.opening.delete(id) }
    if (!existing || existing.agent !== agent) {
      this.bound.set(id, { generation, agent })
      await this.configureContinuity(agent, this.initialContinuityEnabled)
      this.installContext(agent)
    }
    return agent
  }

  private installContext(agent: Agent): void {
    if (this.contextAgents.has(agent)) return
    this.contextAgents.add(agent)
    const id = String(agent.session.id)
    this.disposers.push(agent.ctx.systemPrompt.context({
      name: 'qq-role-life', order: 100, required: true, interpolate: false,
      text: () => this.context.get(id) ?? '',
    }))
    this.disposers.push(agent.ctx.systemPrompt.section({
      name: 'qq-role-rules', order: 0, interpolate: false,
      text: () => this.stableContext.get(id) ?? '',
    }))
  }

  /** Retire prior snapshots inside the open step after obtaining their replacement. */
  private retireRuntimeContexts(agent: Agent, turn: number, step: number): void {
    const session = agent.session
    const snapshots: SessionEvent<'user/message'>[] = []
    for (const seq of session.surface.nodes) {
      // oxlint-disable-next-line typescript/no-deprecated -- Read only retained nodes, not the complete session log.
      const event = session.eventAt(seq)
      if (event?.type === 'user/message' && (String(event.data.source.kind) === 'runtime-context' || event.data.source.kind==='qq-life-projection')) snapshots.push(event)
    }
    if (snapshots.length === 0) return
    const meter = this.ctx.get('tokenMeter')
    if (!meter) throw new QqLifeSessionError('unavailable', 'Runtime context retirement requires the token meter')
    for (const event of snapshots) {
      const seq = event.seq
      const message = session.deriveEventMessage(event)
      // Adjacent shadow-price and replacement records keep pressure accounting
      // and detached replay consistent. Empty developer nodes send no wire text.
      session.append('compaction/prune', {
        shadowedRange: { start: seq, end: seq }, shadowedSeqs: [seq],
        shadowedTokenCount: message === null ? 0 : meter.estimateMessage(message),
      })
      session.append('developer/message', {
        turn, step, message: createDeveloperMessage({ content: [], source: event.data.source }),
      }, { surfaceOp: { op: 'replace', startSeq: seq, endSeq: seq }, sourceEventSeqs: [seq] })
    }
    this.ctx.logger.info(`qq-life: retired ${snapshots.length} runtime context snapshots from model history; original events retained`)
  }

  private async status(item: Admission, agent: Agent): Promise<Record<string, unknown>> {
    const events = await this.events(agent)
    let state = 'accepted', turn: number | undefined, step: number | undefined
    let inboxSeen = false
    let currentTurn: number | undefined, currentStep: number | undefined
    for (const event of events) {
      if (event.type === 'agent/inbox/spliced' && event.data.inserted.some(message => message.id === item.message.id)) inboxSeen = true
      if (event.type === 'step/start') { currentTurn = event.data.turn; currentStep = event.data.step }
      if (event.type === 'user/message' && event.data.id === item.message.id) {
        state = 'consumed'; turn = currentTurn; step = currentStep
      }
      if (event.type === 'turn/end') {
        if (event.data.turn === turn) state = 'settled'
        // A closed turn cannot still own an unsurfaced claimed input.
        // Existing queued messages remain protected by ensureAdmission's inbox check.
        else if (state === 'accepted') inboxSeen = false
      }
    }
    return { state, input_id: item.inputId, message_id: String(item.message.id), turn, step, inbox_seen: inboxSeen }
  }

  private async admit(params: Record<string, unknown>): Promise<Record<string, unknown>> {
    this.load()
    const id = text(params.session_id), iid = text(params.input_id)
    const generation = Number(params.generation)
    if (!Number.isSafeInteger(generation) || generation < 1) throw new Error('invalid_request')
    const key = `${id}\0${iid}`
    const checksum = requestHash('role-input', {
      generation, source: params.source, content: params.content, source_event_ids: params.source_event_ids,
      content_blocks: params.content_blocks,
    })
    let item = this.admitted.get(key)
    if (item && item.digest !== checksum) throw new QqLifeSessionError('idempotency_conflict', 'Input id has different content')
    const agent = await this.bind(id, generation)
    if (!item) {
      const blocks: SessionPromptParams['contentBlocks'] = []
      if (Array.isArray(params.content_blocks)) {
        for (const value of params.content_blocks) {
          const block = record(value)
          if (block.type === 'text') blocks.push({ type: 'text', text: text(block.text) })
          else if (block.type === 'image') {
            const mime = block.mimeType
            if (mime !== 'image/png' && mime !== 'image/jpeg' && mime !== 'image/webp' && mime !== 'image/gif') {
              throw new QqLifeSessionError('invalid_input', 'Unsupported image media type')
            }
            blocks.push({ type: 'image', data: text(block.data), mimeType: mime })
          }
          else throw new QqLifeSessionError('invalid_input', 'Unsupported input content block')
        }
      } else blocks.push({ type: 'text', text: text(params.content) })
      if (blocks.length === 0) throw new QqLifeSessionError('invalid_input', 'Input content cannot be empty')
      const content: ContentBlock[] = this.prepareContent
        ? await this.prepareContent(blocks)
        : blocks.map(block => {
          if (block.type !== 'text') throw new QqLifeSessionError('unavailable', 'Image admission is unavailable')
          return block
        })
      const message = freezeMessage({
        id: brandString<MessageId>(`life-${requestHash(id, iid)}`), role: 'user',
        content,
        source: { kind: 'qq-life', inputId: iid, origin: text(params.source) },
      })
      item = { sessionId: id, inputId: iid, generation, digest: checksum, message,
        mode: params.mode === 'followup' ? 'followup' : 'steer' }
      this.persist(item)
    }
    return this.ensureAdmission(item, agent)
  }

  private async ensureAdmission(item: Admission, agent: Agent): Promise<Record<string, unknown>> {
    const receipt = await this.status(item, agent)
    const pending = [...agent.inbox.nextStep, ...agent.inbox.nextTurn].some(message => message.id === item.message.id)
    // Inbox removal precedes user/message while a request is being prepared.
    // A status query in that window must not insert the same input again.
    if (receipt.state === 'accepted' && !pending && (!receipt.inbox_seen || agent.status !== 'running')) {
      if (item.mode === 'followup') agent.followup(item.message)
      else agent.steer(item.message)
    }
    if (!await this.ctx.sessions.flush(agent.session)) throw new QqLifeSessionError('unavailable', 'Input admission needs durable session storage')
    return this.status(item, agent)
  }

  /**
   * Handle bot session methods. Expected failures return structured categories.
   *
   * @param method - registered qq_life/session method.
   *
   * @param params - wire JSON.
   *
   * @returns receipt or explicit recovery failure; never a fabricated success.
   */
  async handle(method: string, params: Record<string, unknown>): Promise<Record<string, unknown>> {
    try {
      if (method === 'input' || method === 'input_status') {
        const key = `${text(params.session_id)}\0${text(params.input_id)}`
        const prior = this.flights.get(key)
        const flight = Promise.resolve(prior).catch(() => undefined).then(async () => {
          if (method === 'input') return this.admit(params)
          this.load()
          const item = this.admitted.get(key)
          if (!item) return { state: 'not_found' }
          return this.ensureAdmission(item, await this.bind(item.sessionId, item.generation))
        })
        this.flights.set(key, flight)
        try { return await flight } finally { if (this.flights.get(key) === flight) this.flights.delete(key) }
      }
      this.load()
      const id = text(params.session_id)
      const generation = Number(params.generation ?? this.bound.get(id)?.generation ?? this.opening.get(id)
        ?? [...this.admitted.values()].find(item => item.sessionId === id)?.generation)
      if (!Number.isSafeInteger(generation) || generation < 1) {
        throw new QqLifeSessionError('stale_context', 'Session generation is required before first admission')
      }
      const readOnlyCatalog = method === 'catalog' || method === 'methods_changed'
      const liveAgent = readOnlyCatalog ? this.ctx.agents.list().find(candidate => String(candidate.session.id) === id) : undefined
      if (readOnlyCatalog && (!liveAgent || this.bound.get(this.owner(liveAgent)?.root ?? '')?.generation !== generation)) {
        throw new QqLifeSessionError('stale_context', 'Catalog requires an already active bound Agent and generation')
      }
      const agent = liveAgent ?? await this.bind(id, generation)
      if (method === 'methods_changed') {
        if (!this.extensionProtocols.capability) throw new QqLifeSessionError('protocol_mismatch', 'Capability protocol was not negotiated')
        this.invalidateMethods(id)
        return { ok: true }
      }
      if (method === 'catalog') {
        if (!this.extensionProtocols.capability) throw new QqLifeSessionError('protocol_mismatch', 'Capability protocol was not negotiated')
        // Refresh file-backed methods at the read point; this does not wake an Agent.
        this.invalidateMethods(this.owner(agent)!.root)
        const skills = this.ctx.get('skills')
        const methods = skills === undefined ? [] : (await skills.list({ scope: agent }))
          .filter(skill => skill.invocation.modelInvocable)
          .map(skill => ({ name: skill.name, description: skill.description, provider: skill.provider, hash: null }))
        const tools = this.ctx.tools.schemas(agent).map(tool => ({ ...tool, identity: this.toolIdentity(agent, tool.name),
          ...this.extensionProtocols.compact && this.continuityTools.has(tool.name.replace('_','.'))
            ? { documentation: this.continuityTools.get(tool.name.replace('_','.'))!(this.extensionDescription(tool.name.replace('_','.'))).description } : {} }))
        return { ok: true, tools, methods, version: createHash('sha256').update(JSON.stringify({ tools, methods })).digest('hex') }
      }
      if (method === 'usage') return this.resources!.usage(id, Number(params.after_seq ?? -1))
      if (method === 'original') return this.resources!.original(id, text(params.result_ref))
      if (method === 'events') {
        const after = Number(params.after_seq ?? -1), limit = Number(params.limit ?? 200)
        if (!Number.isSafeInteger(after) || after < -1 || !Number.isSafeInteger(limit) || limit < 1 || limit > 2000) throw new Error('invalid_request')
        const events = await this.events(agent)
        if (after >= events.length || (events.length > 0 && events[0]!.seq !== 0)) {
          throw new QqLifeSessionError('history_gap', 'Requested cursor is outside the retained session')
        }
        const page = events.slice(after + 1, after + 1 + limit)
        return { events: page, has_more: after + 1 + page.length < events.length, agent_status: agent.status }
      }
      if (method === 'control') {
        const requested = Array.isArray(params.target_session_ids) ? new Set(params.target_session_ids.map(String)) : undefined
        const fromCalls = this.targetsForCalls(id, params.parent_calls)
        const descendants = new Set(requested ?? [])
        for (const target of fromCalls.targets) requested?.add(target)
        for (const target of fromCalls.descendants) descendants.add(target)
        const selected = requested ?? (params.parent_calls === undefined ? undefined : fromCalls.targets)
        if (selected) {
          for (let changed = true; changed;) {
            changed = false
            for (const candidate of this.ctx.agents.list()) {
              const parent = candidate.session.header.parentSession
              const child = String(candidate.session.id)
              if (parent !== undefined && descendants.has(String(parent)) && !selected.has(child) && this.owner(candidate)?.root === id) {
                selected.add(child); descendants.add(child); changed = true
              }
            }
          }
        }
        const targets = this.ctx.agents.list().filter(candidate => this.owner(candidate)?.root === id
          && (params.mode !== 'attention' || candidate === agent)
          && (selected === undefined || selected.has(String(candidate.session.id))))
        for (const target of targets) target.cancel({ kind: 'user' }, { keepInbox: true })
        if (params.mode === 'attention') {
          await agent.whenIdle()
          const pending = agent.inbox.nextStep[0] ?? agent.inbox.nextTurn[0]
          if (pending) { agent.inbox.remove(pending.id); agent.steer(pending) }
        }
        return { state: targets.every(target => target.status === 'idle') ? 'stopped' : 'cancel_requested',
          sessions: targets.map(target => String(target.session.id)) }
      }
      throw new Error('unknown_method')
    } catch (error: unknown) {
      if (error instanceof QqLifeSessionError) return { ok: false, code: error.code, message: error.message }
      throw error
    }
  }

  /** Remove all contributions after owned Agents have settled. */
  dispose(): void {
    for (const state of this.toolPresentation.values()) for (const dispose of state.disposers) dispose()
    this.toolPresentation.clear()
    for (const registrations of this.internalRegistrations.values()) for (const dispose of registrations) dispose()
    this.internalRegistrations.clear()
    for (const registrations of this.continuityRegistrations.values()) for (const dispose of registrations.disposers) dispose()
    this.continuityRegistrations.clear()
    for (const provider of this.methodProviders.values()) provider.dispose()
    this.methodProviders.clear()
    this.childCalls.clear()
    this.executingCalls.clear()
    this.toolCalls.disable()
    for (const dispose of this.disposers.splice(0).reverse()) dispose()
  }
}
