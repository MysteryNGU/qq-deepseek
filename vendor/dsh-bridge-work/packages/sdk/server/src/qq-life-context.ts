/** Bot request projections: one token meter, persistent reductions, bounded handoff. */
import type { Context } from '@deepseek-ai/cordis'
import type { Agent } from '@deepseek-ai/dsh-agent'
import { createDeveloperMessage, createUserMessage, LlmError } from '@deepseek-ai/dsh-llm'
import { joinContextSections } from '@deepseek-ai/dsh-system-prompt'
import type { SessionEvent, SessionSeq } from '@deepseek-ai/dsh-session'
import { toolPairingBalancedBefore, toolPairingBalancedAfter } from '@deepseek-ai/dsh-compaction'
import type {} from '@deepseek-ai/dsh-compaction-image-offload/projection'
import { createHash } from 'node:crypto'
import { readFileSync } from 'node:fs'

/** Fingerprint of the loaded context policy, including source-mode deployments. */
export const QQ_CONTEXT_ADAPTER_VERSION = createHash('sha256').update(readFileSync(new URL(import.meta.url))).digest('hex')

interface Policy {
  chat_input_target_tokens: number; task_input_target_tokens: number; pressure_ratio: number
  recent_history_target_tokens: number; handoff_target_tokens: number; provider_margin_tokens: number; reference_image_batches: number
}
interface Plan {
  stamp: string; presentation_scope: string; mode: string; protected_input_ids: string[]
  pinned_asset_ids: string[]; protect_task_images: boolean
  optional_context?: { source_text: string; replacement_text: string }
}
interface Prepared { policy: Policy; plan: Plan; currentStart: number; imageStart: number }

function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('invalid context policy record')
  return value as Record<string, unknown>
}

function stable(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stable).join(',')}]`
  if (value && typeof value === 'object') return `{${Object.entries(value).sort(([a],[b]) => a.localeCompare(b))
    .map(([key,item]) => `${JSON.stringify(key)}:${stable(item)}`).join(',')}}`
  return JSON.stringify(value)
}

function hash(value: unknown): string { return createHash('sha256').update(stable(value)).digest('hex') }

/** JSON receipts only; arbitrary external text never declares projection semantics. */
function receipt(event: SessionEvent): Record<string, unknown> | undefined {
  if (event.type !== 'tool/result' || event.data.message.isError) return undefined
  const content = event.data.message.content
  if (content.length !== 1 || content[0]?.type !== 'text') return undefined
  try { return record(JSON.parse(content[0].text)) } catch { return undefined }
}

/** Owns request-local host plans; every retained-message change uses existing session events. */
export class QqLifeContext {
  private readonly plans = new WeakMap<Agent, Prepared>()

  /** Create a request policy owner with optional tool-presentation reset.
   * @param ctx - profile services.
   * @param owns - bot ownership check.
   * @param host - fenced host callback.
   *
   * @param afterHandoff - optional presentation reset; absent leaves the caller's tool state intact.
   */
  constructor(private readonly ctx: Context, private readonly owns: (agent: Agent) => boolean,
    private readonly host: (agent: Agent, body: Record<string, unknown>) => Promise<Record<string, unknown>>,
    private readonly afterHandoff?: (agent: Agent, mode: string) => void) {}

  /** Capture validated host facts for this request.
   * @param agent - request owner.
   * @param reply - host-validated policy and source plan. */
  configure(agent: Agent, reply: Record<string, unknown>): void {
    const policy = record(reply.resource_policy), plan = record(reply.context_plan)
    for (const key of ['chat_input_target_tokens','task_input_target_tokens','recent_history_target_tokens','handoff_target_tokens','provider_margin_tokens','reference_image_batches']) {
      if (typeof policy[key] !== 'number' || !Number.isSafeInteger(policy[key]) || Number(policy[key]) <= 0) throw new Error(`invalid context policy ${key}`)
    }
    if (typeof policy.pressure_ratio !== 'number' || !(policy.pressure_ratio > 0 && policy.pressure_ratio < 1)) throw new Error('invalid pressure ratio')
    if (typeof plan.stamp !== 'string' || typeof plan.presentation_scope !== 'string' || !['chat','task'].includes(String(plan.mode))
      || !Array.isArray(plan.protected_input_ids) || !plan.protected_input_ids.every(item => typeof item === 'string')
      || !Array.isArray(plan.pinned_asset_ids) || !plan.pinned_asset_ids.every(item => typeof item === 'string')
      || typeof plan.protect_task_images !== 'boolean') throw new Error('invalid context source plan')
    const previous = this.plans.get(agent)
    let optional: Plan['optional_context']
    if (plan.optional_context !== undefined) {
      const value=record(plan.optional_context)
      if (typeof value.source_text!=='string' || typeof value.replacement_text!=='string') throw new Error('invalid optional context projection')
      optional={ source_text: value.source_text,replacement_text: value.replacement_text }
    }
    this.plans.set(agent, { currentStart: Number(agent.session.seq),imageStart: previous?.currentStart ?? Number(agent.session.seq),policy: {
      chat_input_target_tokens: Number(policy.chat_input_target_tokens),task_input_target_tokens: Number(policy.task_input_target_tokens),
      pressure_ratio: policy.pressure_ratio,recent_history_target_tokens: Number(policy.recent_history_target_tokens),
      handoff_target_tokens: Number(policy.handoff_target_tokens),
      provider_margin_tokens: Number(policy.provider_margin_tokens),
      reference_image_batches: Number(policy.reference_image_batches),
    }, plan: { stamp: plan.stamp,presentation_scope: plan.presentation_scope,mode: String(plan.mode),
      protected_input_ids: plan.protected_input_ids as string[],pinned_asset_ids: plan.pinned_asset_ids as string[],
      protect_task_images: plan.protect_task_images,...optional===undefined ? {} : { optional_context: optional } } })
  }

  private reduceOptional(agent: Agent,plan: Plan): void {
    const optional=plan.optional_context
    if (!optional || optional.source_text===optional.replacement_text) return
    const event=this.surface(agent).findLast(event => event.type==='user/message' && String(event.data.source.kind)==='runtime-context')
    if (event?.type!=='user/message') return
    const source=record(event.data.source)
    if (source.form!=='snapshot' || !Array.isArray(source.sections)) return
    const sections=source.sections.map(value => {
      const section=record(value)
      if (typeof section.name!=='string' || typeof section.text!=='string') throw new Error('invalid runtime context section')
      return { name: section.name,text: section.text }
    })
    const own=sections.find(section => section.name==='qq-role-life')
    if (!own || own.text!==optional.source_text) throw new LlmError('Runtime facts changed before optional projection.','stale_context',{ status: 409 })
    own.text=optional.replacement_text
    const message=createUserMessage({ content: [{ type: 'text',text: joinContextSections(sections) }],source: { kind: 'qq-life-projection' } })
    const original=agent.session.deriveEventMessage(event)
    const meter=this.ctx.tokenMeter
    if (!original || meter.estimateMessage(message)>=meter.estimateMessage(original)) return
    const seq=event.seq
    agent.session.append('compaction/prune',{ shadowedRange: { start: seq,end: seq },shadowedSeqs: [seq],
      shadowedTokenCount: meter.estimateMessage(original) })
    agent.session.append('user/message',message,{ surfaceOp: { op: 'replace',startSeq: seq,endSeq: seq },sourceEventSeqs: [seq] })
  }

  private surface(agent: Agent): SessionEvent[] {
    return agent.session.surface.nodes.map(seq => {
      // oxlint-disable-next-line typescript/no-deprecated -- Retained surface only.
      const event = agent.session.eventAt(seq)
      if (!event) throw new Error('missing context surface event')
      return event
    })
  }

  private deduplicate(agent: Agent, turn: number, step: number, plan: Plan): void {
    const events = this.surface(agent), seen = new Map<string, { call: SessionEvent<'assistant/message'>; result: SessionEvent<'tool/result'> }>()
    for (let i = events.length - 1; i > 0; i--) {
      const result = events[i], call = events[i - 1]
      if (result?.type !== 'tool/result' || call?.type !== 'assistant/message') continue
      const calls = call.data.message.content.filter(block => block.type === 'tool-call')
      if (calls.length !== 1 || calls[0]?.name !== 'life_read' || calls[0].id !== result.data.message.toolCallId) continue
      if (call.data.message.content.some(block => block.type === 'text' && block.text.trim())) continue
      const data = receipt(result), projection = data?.read_projection
      if (!projection || typeof projection !== 'object') continue
      const meta = record(projection)
      if (!meta.version || !meta.tool_identity || meta.scope !== plan.presentation_scope) continue
      let args: unknown
      try { args=JSON.parse(calls[0].arguments) } catch { continue }
      const key = hash({ tool: calls[0].name, args, scope: meta.scope,
        version: meta.version, result: meta.result_hash,
        identity: meta.tool_identity })
      const latest = seen.get(key)
      if (!latest) { seen.set(key, { call, result }); continue }
      const seqs = [call.seq, result.seq]
      if (!toolPairingBalancedBefore(agent.session, call.seq) || !toolPairingBalancedAfter(agent.session, result.seq)) continue
      const meter = this.ctx.tokenMeter
      const shadowedTokenCount = seqs.reduce((sum,seq) => {
        // oxlint-disable-next-line typescript/no-deprecated -- Selected retained nodes.
        const event = agent.session.eventAt(seq)!
        const message = agent.session.deriveEventMessage(event)
        return sum + (message ? meter.estimateMessage(message) : 0)
      },0)
      agent.session.append('compaction/prune', { shadowedRange: { start: call.seq, end: result.seq }, shadowedSeqs: seqs, shadowedTokenCount })
      agent.session.append('developer/message', { turn, step, message: createDeveloperMessage({
        content: [{ type: 'text', text: `Repeated verified read: latest tool result seq=${latest.result.seq}; original call/result seqs=${seqs.join(',')}. Full read: ${stable(args)}; version=${String(meta.version)}.` }],
        source: { kind: 'qq-life-projection' },
      }) }, { surfaceOp: { op: 'replace', startSeq: call.seq, endSeq: result.seq }, sourceEventSeqs: seqs })
    }
  }

  private offload(agent: Agent, plan: Plan, currentStart: number, referenceBatches: number): void {
    if (plan.protect_task_images) return
    const targets: { seq: SessionSeq; imageIndexes: number[] }[] = []
    const surface=this.surface(agent)
    const recent=new Set(surface.filter(event => (event.type==='user/message' || event.type==='tool/result')
      && agent.session.deriveEventMessage(event)?.content.some(block => block.type==='image' && !block.offloaded))
      .slice(-referenceBatches).map(event => event.seq))
    for (const event of surface) {
      if (recent.has(event.seq)) continue
      if (event.seq >= currentStart || event.type !== 'user/message' && event.type !== 'tool/result') continue
      if (event.type === 'user/message' && event.data.source.kind === 'qq-life'
        && plan.protected_input_ids.includes(event.data.source.inputId)) continue
      const message = agent.session.deriveEventMessage(event)
      if (!message) continue
      const text = message.content.flatMap(block => block.type === 'text' ? [block.text] : []).join('\n')
      if (plan.pinned_asset_ids.some(id => text.includes(id))) continue
      let index = 0
      const imageIndexes = message.content.flatMap(block => {
        if (block.type !== 'image') return []
        const current = index++
        return block.offloaded ? [] : [current]
      })
      if (imageIndexes.length) targets.push({ seq: event.seq, imageIndexes })
    }
    if (targets.length) agent.session.append('image/offload', { targets })
  }

  private handoffRange(agent: Agent, plan: Plan, currentStart: number, recentTokens: number): { start: SessionSeq; end: SessionSeq; tokens: number } | undefined {
    const events = this.surface(agent), priced = new Map(this.ctx.tokenMeter.measure(agent.session).nodes.map(node => [node.seq,node.tokens]))
    let retained = 0, tail = events.length
    while (tail > 0 && retained < recentTokens) { tail--; retained += priced.get(events[tail]!.seq) ?? 0 }
    let start: number | undefined, best: { start: SessionSeq; end: SessionSeq; tokens: number } | undefined, tokens = 0
    for (let i = 0; i < tail; i++) {
      const event = events[i]!
      let protectedNode = event.seq >= currentStart || event.type === 'system/message'
      if (event.type === 'user/message' && (String(event.data.source.kind) === 'runtime-context' || event.data.source.kind==='qq-life-projection'
        || event.data.source.kind === 'qq-life' && plan.protected_input_ids.includes(event.data.source.inputId))) protectedNode = true
      const message = agent.session.deriveEventMessage(event)
      if (message?.content.some(block => block.type === 'image' && !block.offloaded)) protectedNode = true
      if (event.type === 'tool/result') {
        const body = message?.content.flatMap(block => block.type === 'text' ? [block.text] : []).join('') ?? ''
        if (event.data.message.isError || /"status"\s*:\s*"(?:unknown|dispatched|dispatching|stopping)"/.test(body)) protectedNode = true
      }
      if (protectedNode) { start = undefined; tokens = 0; continue }
      if (start === undefined) {
        if (!toolPairingBalancedBefore(agent.session,event.seq)) continue
        start = i; tokens = 0
      }
      tokens += priced.get(event.seq) ?? 0
      if (i > start && toolPairingBalancedAfter(agent.session,event.seq) && tokens > (best?.tokens ?? 0)) {
        best = { start: events[start]!.seq, end: event.seq, tokens }
      }
    }
    return best
  }

  /** Prepare the logged surface with at most one handoff, preserving cancellation.
   * @param payload - complete logged request position.
   */
  async prepare(payload: { agent: Agent; turn: number; step: number; signal: AbortSignal }): Promise<void> {
    const { agent, turn, step, signal } = payload
    signal.throwIfAborted()
    if (!this.owns(agent)) return
    const prepared = this.plans.get(agent)
    if (!prepared) throw new Error('missing host context plan')
    const { policy, plan, currentStart, imageStart } = prepared, session = agent.session
    const header = session.requestHeader()
    if (!header) throw new Error('missing complete request envelope')
    const info = session.requestContext(), model = await this.ctx.llm.resolveModelInfo(header.config.provider,header.config.model,signal)
    const output = header.config.maxTokens ?? model.defaultMaxTokens ?? 0
    const capacity = info?.contextWindow === undefined ? undefined : Math.max(0,info.contextWindow-output-policy.provider_margin_tokens)
    const target = Math.min(plan.mode === 'task' ? policy.task_input_target_tokens : policy.chat_input_target_tokens,capacity ?? Infinity)
    const meter = this.ctx.tokenMeter
    let measured = meter.measure(session)
    if (measured.totalTokens > target * policy.pressure_ratio) {
      this.deduplicate(agent,turn,step,plan)
      this.offload(agent,plan,imageStart,policy.reference_image_batches)
      measured = meter.measure(session)
    }
    if (measured.totalTokens > target) {
      this.reduceOptional(agent,plan)
      measured=meter.measure(session)
    }
    if (measured.totalTokens > target) {
      const range = this.handoffRange(agent,plan,currentStart,policy.recent_history_target_tokens)
      const compaction = this.ctx.get('compaction')
      if (range && range.tokens > policy.handoff_target_tokens && compaction) {
        let handedOff=false
        try {
          await compaction.compactRegion(range.start,range.end,agent,signal)
          handedOff=true
        } catch (error) {
          signal.throwIfAborted()
          this.ctx.logger.warn(`qq-life handoff unavailable; original protected context retained: ${String(error)}`)
        }
        if (handedOff) this.afterHandoff?.(agent,plan.mode)
        measured = meter.measure(session)
      }
    }
    signal.throwIfAborted()
    const valid = await this.host(agent,{ kind: 'context_validate', stamp: plan.stamp })
    signal.throwIfAborted()
    if (valid.ok !== true) throw new LlmError('Sources changed during request preparation; yield to the scheduler and rebuild current facts.','stale_context',{ status: 409 })
    if (capacity !== undefined && measured.totalTokens > capacity) {
      throw new LlmError(`Required input exceeds physical capacity: estimated=${measured.totalTokens}, capacity=${capacity}, output=${output}. Read original sources with life_read(kind:life_source|observation,ref/event_id,cursor) in bounded pages; protected inputs were retained.`, 'context_capacity_blocked', { status: 400 })
    }
    // Mandatory input is allowed above the working target; this never concludes a turn.
    if (measured.totalTokens > target) this.ctx.logger.info(`qq-life mandatory_overflow: ${measured.totalTokens} > ${target}`)
  }
}

declare module '@deepseek-ai/dsh-llm' {
  interface MessageSourceMap {
    /** Host-approved optional context or a reference to an exact verified read pair.
     * @persistenceAttribution
     */
    'qq-life-projection': { kind: 'qq-life-projection' }
  }
}
