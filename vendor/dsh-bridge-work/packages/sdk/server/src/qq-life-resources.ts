/** Bot-scoped stream accounting and recoverable text presentation; canonical tool values stay intact. */
import type { Context } from '@deepseek-ai/cordis'
import type { Agent } from '@deepseek-ai/dsh-agent'
import type { ContentBlock, TokenUsage } from '@deepseek-ai/dsh-llm'
import { LlmError } from '@deepseek-ai/dsh-llm'
import type { ToolExecution, ToolExecutionResult, PostToolDecision } from '@deepseek-ai/dsh-tools'
import type {} from '@deepseek-ai/dsh-spill'
import { appendFileSync, existsSync, mkdirSync, readFileSync, openSync, fsyncSync, closeSync, truncateSync } from 'node:fs'
import { dirname, isAbsolute, join } from 'node:path'
import { createHash, randomUUID } from 'node:crypto'

interface Owner { root: string; child: boolean }
interface Policy { text_preview_target_tokens: number; text_result_max_tokens: number; text_result_max_utf8_bytes: number }
interface Original { root: string; session: string; call: string; locator: string; hash: string; bytes: number }
interface Call extends Record<string, unknown> {
  call_key: string; session_id: string; root_session_id: string; revision: number
}

const estimate = (text: string): number => Math.ceil(Buffer.byteLength(text, 'utf8') / 2)

/** Owns only the bot sessions identified by the SDK adapter's runtime lineage. */
export class QqLifeResources {
  private readonly policies = new Map<string, Policy>()
  private readonly positions = new Map<string, { turn: number; step: number }>()
  private readonly inputs = new Map<string, { turn: number; refs: string[] }>()
  private readonly attempts = new Map<string, number>()
  private readonly entries: Call[] = []
  private readonly originals = new Map<string, Original>()
  private loaded = false
  private accountingWarning = false
  private journalWritable = true

  /** @param ctx - running profile. @param home - bot private home. @param owner - authoritative runtime ownership.
   * @param budget - host admission/settlement callback; omitted only for accounting-only standalone consumers.
   * Rejected admission never reaches the adapter. Failed settlement retains the host's durable reservation.
   */
  constructor(private readonly ctx: Context, private readonly home: () => string,
    private readonly owner: (agent: Agent) => Owner | undefined,
    private readonly budget?: (agent: Agent, kind: 'budget_reserve' | 'budget_settle', record: Record<string, unknown>) => Promise<Record<string, unknown>>) {}

  private file(name: string): string { return join(this.home(), 'qq-life', name) }

  private append(name: string, value: object): void {
    const path = this.file(name)
    mkdirSync(dirname(path), { recursive: true })
    const fd = openSync(path, 'a')
    try { appendFileSync(fd, `${JSON.stringify(value)}\n`, 'utf8'); fsyncSync(fd) } finally { closeSync(fd) }
  }

  private load(): void {
    if (this.loaded) return
    for (const [name, consume] of [
      ['usage.jsonl', (item: Call) => {
        this.entries.push(item)
        const key = `${item.session_id}:${String(item.turn)}:${String(item.step)}:${String(item.purpose)}`
        if (typeof item.attempt === 'number' && Number.isSafeInteger(item.attempt)) this.attempts.set(key, Math.max(this.attempts.get(key) ?? 0, item.attempt))
      }],
      ['results.jsonl', (item: Original & { ref: string }) => { this.originals.set(item.ref, item) }],
    ] as const) {
      const path = this.file(name)
      if (!existsSync(path)) continue
      const body = readFileSync(path, 'utf8')
      const lines = body.split('\n')
      for (let index = 0; index < lines.length; index++) {
        if (!lines[index]!.trim()) continue
        try { consume(JSON.parse(lines[index]!)) } catch (error: unknown) {
          if (index === lines.length - 1 && !body.endsWith('\n')) {
            truncateSync(path, Buffer.byteLength(body.slice(0, body.lastIndexOf('\n') + 1)))
            break
          }
          throw new Error(`Unreadable bot resource journal ${name}:${index + 1}: ${String(error)}`)
        }
      }
    }
    this.loaded = true
  }

  private loadAccounting(): void {
    try { this.load() } catch (error: unknown) {
      this.accountingWarning = true; this.journalWritable = false; this.loaded = true
      this.ctx.logger.warn(`Bot accounting history incomplete; model execution continues: ${String(error)}`)
    }
  }

  private settleCall(call: Call): void {
    if (this.journalWritable) {
      try { this.append('usage.jsonl', call) } catch (error: unknown) {
        this.accountingWarning = true; this.journalWritable = false
        this.ctx.logger.warn(`Bot usage persistence unavailable; totals are incomplete: ${String(error)}`)
      }
    }
    this.entries.push(call)
  }

  /** Apply the host-validated policy and actual request position before model execution.
   * @param agent - bound role or descendant. @param raw - host configuration. @param position - loop-owned request position.
   */
  configure(agent: Agent, raw: unknown, position: { turn: number; step: number }): void {
    const owned = this.owner(agent)
    if (!owned) return
    this.positions.set(String(agent.session.id), position)
    if (raw === undefined) return
    if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) throw new Error('invalid resource policy')
    const data = raw as Record<string, unknown>
    for (const key of ['text_preview_target_tokens', 'text_result_max_tokens', 'text_result_max_utf8_bytes']) {
      if (typeof data[key] !== 'number' || !Number.isSafeInteger(data[key]) || data[key] <= 0) throw new Error(`invalid resource policy: ${key}`)
    }
    this.policies.set(owned.root, {
      text_preview_target_tokens: Number(data.text_preview_target_tokens), text_result_max_tokens: Number(data.text_result_max_tokens),
      text_result_max_utf8_bytes: Number(data.text_result_max_utf8_bytes),
    })
  }

  /** Capture actual admitted inputs, never later pending messages.
   * @param agent - owned role. @param turn - current turn. @param refs - loop-admitted input identities.
   */
  admitted(agent: Agent, turn: number, refs: string[]): void {
    const id = String(agent.session.id), old = this.inputs.get(id)
    this.inputs.set(id, { turn, refs: [...new Set([...(old?.turn === turn ? old.refs : []), ...refs])] })
  }

  /** Register one observer per actual SDK-owned stream call, including summary retries.
   * @returns disposer for the stream observer.
   */
  observeCalls(): () => void {
    const resources = this
    return this.ctx.on('llm/stream', async function* (options, next) {
      const agent = options.sessionId === undefined ? undefined : resources.ctx.agents.get(options.sessionId)
      const owned = agent === undefined ? undefined : resources.owner(agent)
      if (!owned || !agent) { yield* next(); return }
      resources.loadAccounting()
      const call: Call = { call_key: `sdk:${randomUUID()}`, session_id: String(agent.session.id), root_session_id: owned.root,
        revision: 1, provider: options.provider, model: options.model, started_at: Date.now() / 1000,
        status: 'started', purpose: options.purpose ?? 'ordinary', ...resources.positions.get(String(agent.session.id)),
        context_estimate: estimate(JSON.stringify(options.messages)) }
      const key = `${call.session_id}:${String(call.turn)}:${String(call.step)}:${String(call.purpose)}`
      call.attempt = (resources.attempts.get(key) ?? 0) + 1
      resources.attempts.set(key, Number(call.attempt))
      call.input_refs = resources.inputs.get(call.session_id)?.refs ?? resources.inputs.get(owned.root)?.refs ?? []
      // The owning host persists a reservation before any provider work. No
      // callback means accounting-only (standalone presentation consumers).
      if (resources.budget) {
        const reply = await resources.budget(agent, 'budget_reserve', { ...call, max_tokens: options.maxTokens ?? null })
        if (reply.ok !== true) throw new LlmError(String(reply.message ?? 'Bot spending admission failed'), String(reply.code ?? 'budget_unavailable'), { status: 400 })
      }
      resources.settleCall(call)
      let usage: TokenUsage | undefined, status = 'unknown'
      try {
        for await (const chunk of next()) {
          if (chunk.type === 'usage') usage = chunk.usage
          if (chunk.type === 'finish') status = chunk.reason.kind === 'error' ? 'failed' : chunk.reason.kind === 'aborted' ? 'cancelled' : 'completed'
          yield chunk
        }
      } finally {
        const final: Call = { ...call, revision: 2, status, finished_at: Date.now() / 1000,
          ...usage === undefined ? {} : { usage } }
        resources.settleCall(final)
        if (resources.budget) {
          try {
            const reply = await resources.budget(agent, 'budget_settle', final)
            if (reply.ok !== true) throw new Error(String(reply.code ?? 'budget_settlement_failed'))
          } catch (error: unknown) {
            // The durable reservation remains charged, including after a
            // process loss. A projection failure cannot reopen spending room.
            resources.ctx.logger.error(`Bot spending settlement unavailable; reservation retained: ${String(error)}`)
          }
        }
      }
    })
  }

  /** Return a bounded page of durable call settlements owned by one bot root.
   * @param root - current bound root. @param after - last global journal cursor.
   * @returns records and a cursor; unfinished calls remain visible as unknown usage.
   */
  usage(root: string, after: number): Record<string, unknown> {
    this.loadAccounting()
    if (!Number.isSafeInteger(after) || after < -1 || after >= this.entries.length && after !== -1) throw new Error('invalid usage cursor')
    const end = Math.min(this.entries.length, after + 201)
    return { records: this.entries.slice(after + 1, end).filter(item => item.root_session_id === root),
      cursor: end - 1, has_more: end < this.entries.length, accounting_warning: this.accountingWarning }
  }

  /** Bound only model-visible text. PTC canonical values and multimodal results retain their original semantics.
   * @param exec - actual execution. @param content - accepted display text. @param isError - original error status. @param facts - structured execution facts.
   * @returns preview content, or original content when recovery storage fails.
   */
  async present(exec: ToolExecution, content: ContentBlock[], isError: boolean, facts: Record<string, unknown> = {}): Promise<ContentBlock[]> {
    const owned = exec.agent === undefined ? undefined : this.owner(exec.agent)
    const policy = owned === undefined ? undefined : this.policies.get(owned.root)
    if (!owned || !policy || !exec.agent || !content.every(block => block.type === 'text')) return content
    const args = exec.arguments
    if (exec.name === 'life_read' && args !== null && typeof args === 'object' && 'proposal' in args) {
      const proposal = args.proposal
      if (proposal !== null && typeof proposal === 'object' && 'kind' in proposal && proposal.kind === 'observation') return content
    }
    const body = content.map(block => block.type === 'text' ? block.text : '').join('\n')
    if (estimate(body) <= policy.text_preview_target_tokens && Buffer.byteLength(body, 'utf8') <= policy.text_result_max_utf8_bytes) return content
    try {
      const store = this.ctx.get('spillStore')
      if (!store) throw new Error('no spill store')
      const saved = await store.saveText({ owner: { sessionId: exec.agent.session.id },
        source: { kind: 'tool', toolName: exec.name, callId: exec.callId, label: 'result' }, suggestedName: `${exec.name}.txt`, content: body })
      // This SDK profile uses spill-local. A non-file backend needs its own
      // reader before it can participate in recoverable bounded expansion.
      if (!isAbsolute(saved.locator) || readFileSync(saved.locator, 'utf8') !== body) throw new Error('spill is not locally recoverable')
      const hash = createHash('sha256').update(body).digest('hex')
      const ref = `result:${exec.agent.session.id}:${String(exec.callId)}:${hash}`
      const original = { root: owned.root, session: String(exec.agent.session.id), call: String(exec.callId), locator: String(saved.locator), hash, bytes: saved.bytes }
      this.load(); this.append('results.jsonl', { ref, ...original }); this.originals.set(ref, original)
      const lines = body.split('\n')
      const head = lines.slice(0, 12).map((text, i) => ({ line: i + 1, text }))
      const tail = lines.slice(Math.max(12, lines.length - 12)).map((text, i) => ({ line: Math.max(12, lines.length - 12) + i + 1, text }))
      const envelope = { tool: exec.name, status: isError || typeof facts.exit_code === 'number' && facts.exit_code !== 0 ? 'failed' : 'completed', is_error: isError, ...facts,
        coverage: 'partial', source_coverage: 'tool_report', total_lines: lines.length, content_hash: hash, result_ref: ref,
        preview: [...head, ...tail], estimate_kind: 'utf8_estimate', next_read: { kind: 'observation', result_ref: ref, mode: 'lines' } }
      // Include status and reference in the budget; very long single lines are
      // clipped as text inside a complete JSON envelope, never broken JSON.
      let serialized = JSON.stringify(envelope)
      while (estimate(serialized) > policy.text_result_max_tokens || Buffer.byteLength(serialized, 'utf8') > policy.text_result_max_utf8_bytes) {
        if (envelope.preview.length > 2) envelope.preview.splice(Math.floor(envelope.preview.length / 2), 1)
        else if (envelope.preview.some(item => item.text.length > 16)) envelope.preview = envelope.preview.map(item => ({ ...item, text: item.text.slice(0, Math.floor(item.text.length / 2)) }))
        else throw new Error('resource envelope cannot fit configured budget')
        serialized = JSON.stringify(envelope)
      }
      return [{ type: 'text', text: serialized }]
    } catch (error: unknown) {
      this.ctx.logger.warn(`Bot text preview unavailable; retaining original result: ${String(error)}`)
      return content
    }
  }

  /** Present accepted text after canonical program values have been retained.
   * @param exec - execution. @param result - original canonical outcome. @param decision - downstream policy decision.
   * @returns the unchanged program value decision or a text-only display replacement.
   */
  async post(exec: ToolExecution, result: Readonly<ToolExecutionResult>, decision: PostToolDecision): Promise<PostToolDecision> {
    if (decision.kind !== 'accept' || 'value' in decision || exec.parent !== undefined) return decision
    const content = result.content
    if (decision.content && decision.content !== content) return decision
    const value = result.isError ? result.error : result.value
    const facts: Record<string, unknown> = {}
    if (value !== null && typeof value === 'object' && !Array.isArray(value)) {
      const record = value as Record<string, unknown>
      const exit = record.exit_code ?? record.exitCode
      if (typeof exit === 'number' && Number.isFinite(exit)) facts.exit_code = exit
      if (typeof record.code === 'string' || typeof record.code === 'number') facts.error_code = record.code
      if (Array.isArray(record.diagnostics)) facts.diagnostics = record.diagnostics.slice(0, 5)
      if (result.isError && typeof record.message === 'string') facts.error_message = record.message
    }
    return { kind: 'accept', ...decision.additionalContexts === undefined ? {} : { additionalContexts: decision.additionalContexts },
      content: await this.present(exec, content, result.isError, facts) }
  }

  /** Read only a previously recorded local spill owned by this exact bot root.
   * @param root - bound owner. @param ref - opaque preview reference.
   * @returns full text to the host reader; missing/expired storage is explicit.
   */
  original(root: string, ref: string): Record<string, unknown> {
    this.load()
    const record = this.originals.get(ref)
    if (!record || record.root !== root) return { ok: false, code: 'scope_mismatch' }
    if (!existsSync(record.locator)) return { ok: false, code: 'expired' }
    const text = readFileSync(record.locator, 'utf8')
    if (createHash('sha256').update(text).digest('hex') !== record.hash) return { ok: false, code: 'stale_context' }
    return { text, content_hash: record.hash, coverage: 'tool_report' }
  }
}
