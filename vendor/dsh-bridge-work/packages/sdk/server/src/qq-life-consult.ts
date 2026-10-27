/** Bot-owned consultation job producer; host callbacks own durable business state. */
import type { Context } from '@deepseek-ai/cordis'
import type { Agent } from '@deepseek-ai/dsh-agent'
import type { ToolExecution } from '@deepseek-ai/dsh-tools'
import type { JobId, JobOutcome } from '@deepseek-ai/dsh-jobs'
import type { SubprocessHandle } from '@deepseek-ai/dsh-subprocess'
import { readFileSync } from 'node:fs'
import { createHash } from 'node:crypto'

/** Fingerprint of the loaded producer, valid for both source and bundled modules. */
export const QQ_CONSULT_ADAPTER_VERSION = createHash('sha256').update(readFileSync(new URL(import.meta.url))).digest('hex')

declare module '@deepseek-ai/dsh-jobs' {
  interface JobKindMap {
    /** Dedicated bot consultation; final answers arrive through the host's durable inbox. */
    codex_consult: 'codex_consult'
  }
}

type Callback = (agent: Agent, body: Record<string, unknown>) => Promise<Record<string, unknown>>

/** Expected admission or host-protocol failure; no model request is started by this error. */
export class QqLifeConsultError extends Error {
  /** @param code - recovery category. @param message - operation-specific diagnostic. */
  constructor(readonly code: 'invalid_spec' | 'unavailable' | 'binding_failed', message: string) {
    super(message)
    this.name = 'QqLifeConsultError'
  }
}

function spec(value: Record<string, unknown>): {
  requestId: string; argv: string[]; cwd: string; graceMs: number; timeoutMs: number
} {
  if (typeof value.request_id !== 'string' || !Array.isArray(value.argv)
    || !value.argv.every((item: unknown) => typeof item === 'string') || value.argv.length === 0
    || typeof value.cwd !== 'string' || typeof value.grace_ms !== 'number'
    || !Number.isSafeInteger(value.grace_ms) || value.grace_ms <= 0
    || typeof value.timeout_ms !== 'number' || !Number.isSafeInteger(value.timeout_ms)
    || value.timeout_ms <= value.grace_ms || value.timeout_ms > 2_147_483_647) {
    throw new QqLifeConsultError('invalid_spec', 'invalid consultation host execution spec')
  }
  return { requestId: value.request_id, argv: value.argv, cwd: value.cwd,
    graceMs: value.grace_ms, timeoutMs: value.timeout_ms }
}

/** Owns live process handles only; persistent identifiers and results stay in the bot host. */
export class QqLifeConsult {
  private readonly executions = new Map<string, { job: JobId; process: SubprocessHandle; owner: Agent }>()

  /** @param ctx - mounted runtime. @param callback - scoped host callback with trusted Agent identity. */
  constructor(private readonly ctx: Context, private readonly callback: Callback) {}

  /**
   * Submit or control one consultation. Host rejects unsupported audiences and duplicate launches.
   * @param agent - actual invoking Agent. @param proposal - model JSON. @param exec - creating tool invocation.
   * @returns short admission/control receipt; the answer is never awaited by the tool.
   */
  async execute(agent: Agent, proposal: unknown, exec: ToolExecution): Promise<Record<string, unknown>> {
    const reply = await this.callback(agent, { kind: 'life.consult', proposal, call_id: String(exec.callId) })
    if (reply.ok === false) return reply
    const action = typeof proposal === 'object' && proposal !== null && 'action' in proposal ? proposal.action : 'ask'
    if (action === 'cancel' && typeof reply.request_id === 'string') {
      const owned = this.executions.get(reply.request_id)
      if (owned && owned.owner === agent) {
        const jobs = this.ctx.get('jobs')
        if (!jobs) throw new Error('consultation job registry disappeared')
        jobs.kill(owned.job, agent.session.id, 'consultation cancelled')
      }
      return reply
    }
    if (reply.launch !== true) return reply
    const frozen = spec(reply)
    const jobs = this.ctx.get('jobs')
    const processes = this.ctx.get('subprocess')
    if (!jobs || !processes) {
      await this.callback(agent, { kind: 'consult_settle', request_id: frozen.requestId,
        call_id: String(exec.callId), stopped: true })
      throw new QqLifeConsultError('unavailable', 'consultation requires mounted jobs and subprocess providers')
    }
    let handle: SubprocessHandle | undefined
    let stopped = false
    let exitCode: number | null = null
    let cancelled = false
    const job = jobs.start({
      kind: 'codex_consult', owner: agent.session.id, label: `Codex consultation ${frozen.requestId}`,
      toolCall: { callId: exec.callId, rootCallId: exec.callId }, outputLimitBytes: 4096,
      run: () => {
        const process = processes.spawn({ argv: frozen.argv, cwd: frozen.cwd, graceMs: frozen.graceMs,
          stdio: { stdin: 'pipe', stdout: { maxBytes: 8192 }, stderr: { maxBytes: 8192 } },
          env: { PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' } })
        handle = process
        let escalation: ReturnType<typeof setTimeout> | undefined
        const done: Promise<JobOutcome> = (async () => {
          try {
            const outcome = await process.done
            exitCode = outcome.exitCode
            stopped = await process.waitForExit(AbortSignal.timeout(frozen.graceMs))
            if (!stopped) throw new Error('consultation process range not stopped')
            return { status: cancelled ? 'killed' : outcome.exitCode === 0 ? 'completed' : 'failed' }
          } catch (error) {
            process.terminate()
            try { stopped = await process.waitForExit(AbortSignal.timeout(frozen.graceMs)) }
            catch (stopError) { return { status: 'failed', detail: `stop unknown: ${String(stopError)}` } }
            return { status: 'failed', detail: String(error) }
          } finally {
            if (escalation !== undefined) clearTimeout(escalation)
          }
        })()
        return { done, cancel: () => {
          if (cancelled) return
          cancelled = true
          process.stdin?.write(JSON.stringify({ action: 'cancel' })+'\n')
          escalation = setTimeout(() => process.terminate(), frozen.graceMs)
        } }
      },
    })
    if (!handle) throw new Error('consultation producer did not return a process')
    const process = handle
    this.executions.set(frozen.requestId, { job, process, owner: agent })
    // Install the collector before granting start: settlement is collected by this owner,
    // so the generic tool-jobs controller cannot emit a second answer notice.
    const collect = async (): Promise<void> => {
      try {
        let view = await jobs.wait(job, frozen.timeoutMs, agent.session.id)
        while (view.status === 'running' || view.status === 'stopping') {
          jobs.kill(job, agent.session.id, 'consultation deadline')
          view = await jobs.wait(job, frozen.graceMs, agent.session.id)
        }
        await this.callback(agent, { kind: 'consult_settle', request_id: frozen.requestId,
          call_id: String(exec.callId), stopped, exit_code: exitCode })
      } finally {
        this.executions.delete(frozen.requestId)
      }
    }
    void collect().catch(error => this.ctx.logger('qq-life-consult').warn('Host settlement deferred to durable recovery: %s', String(error)))
    try {
      const bound = await this.callback(agent, { kind: 'consult_bind', request_id: frozen.requestId,
        job_id: String(job), call_id: String(exec.callId) })
      if (bound.ok === false) throw new QqLifeConsultError('binding_failed', JSON.stringify(bound))
      if (!process.stdin) throw new Error('consultation worker has no start pipe')
      process.stdin.on('error', error => this.ctx.logger('qq-life-consult').warn('Consultation control pipe: %s', String(error)))
      process.stdin.write(JSON.stringify({ action: 'start', request_id: frozen.requestId, job_id: String(job) })+'\n')
      const { argv: _argv, cwd: _cwd, grace_ms: _grace, timeout_ms: _timeout, launch: _launch, ...receipt } = reply
      return { ...receipt, job_id: String(job), status: 'queued', phase: 'job_bound' }
    } catch (error) {
      process.terminate()
      throw error
    }
  }
}
