/**
 * Durable ledger of externally driven QQ-life tool operations.
 *
 * The bridge decides which tool to run; this module only remembers what was
 * asked, what came back, and whether the worker ever settled it — so a restart
 * can reconcile instead of repeating an install or a file edit.
 *
 * @module @deepseek-ai/dsh-sdk-jsonrpc-server/qq-life-operations
 */

import { createHash } from 'node:crypto'
import { appendFileSync, existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs'
import { dirname } from 'node:path'
import type { ExternalToolResult } from '@deepseek-ai/dsh-agent'

/** Lifecycle of one bridge operation, as reported back to the caller. */
export type QqLifeOperationStatus =
  | 'prepared' | 'dispatching' | 'stopping' | 'result_recorded'
  | 'denied' | 'failed_before_dispatch' | 'cancelled' | 'unknown'

/** One externally authored QQ life operation and its settled result. */
export interface QqLifeOperation {
  readonly operationId: string
  readonly sessionId: string
  readonly tool: string
  readonly arguments: unknown
  readonly requestHash: string
  status: QqLifeOperationStatus
  result?: ExternalToolResult
  error?: string
  readonly startedAt: number
  settledAt?: number
}

/** Statuses that no longer change without a new dispatch. */
const TERMINAL_STATUSES: ReadonlySet<QqLifeOperationStatus> = new Set([
  'result_recorded', 'denied', 'failed_before_dispatch', 'cancelled',
])

/**
 * Whether a status is final.
 * @param status - operation status.
 * @returns true when the operation will not change by itself.
 */
export function isSettledStatus(status: QqLifeOperationStatus): boolean {
  return TERMINAL_STATUSES.has(status)
}

/**
 * Canonical JSON with sorted object keys, so one request always hashes alike.
 * @param value - any JSON-ish value.
 * @returns stable serialization.
 */
function canonicalJson(value: unknown): string {
  if (value === null || typeof value !== 'object') return JSON.stringify(value) ?? 'null'
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`
  const entries = Object.entries(value as Record<string, unknown>)
    .filter(([, item]) => item !== undefined)
    .sort(([left], [right]) => left.localeCompare(right))
  return `{${entries.map(([key, item]) => `${JSON.stringify(key)}:${canonicalJson(item)}`).join(',')}}`
}

/**
 * Fingerprint one tool request.
 * @param tool - tool name.
 * @param args - normalized arguments object.
 * @returns hex sha256 of the canonical request.
 */
export function requestHash(tool: string, args: unknown): string {
  return createHash('sha256').update(canonicalJson({ tool, arguments: args })).digest('hex')
}

/**
 * Append-only operation log. Every mutation is flushed before the dispatch is
 * acknowledged, and replay is last-write-wins per operation id.
 */
export class QqLifeOperationLog {
  private readonly operations = new Map<string, QqLifeOperation>()
  private lines = 0

  /**
   * @param file - JSONL path; created on first write.
   */
  constructor(private readonly file: string) {}

  /**
   * Read the log from disk and mark anything still in flight as unknown,
   * because this worker never saw those operations settle.
   * @returns how many interrupted operations were recovered.
   */
  load(): number {
    if (!existsSync(this.file)) return 0
    let raw: string
    try {
      raw = readFileSync(this.file, 'utf8')
    } catch {
      return 0
    }
    for (const line of raw.split('\n')) {
      const trimmed = line.trim()
      if (trimmed.length === 0) continue
      try {
        const record = JSON.parse(trimmed) as { op?: QqLifeOperation }
        if (record.op?.operationId) this.operations.set(record.op.operationId, record.op)
      } catch {
        // A truncated trailing line is what a crash leaves behind; skip it.
      }
    }
    let recovered = 0
    for (const operation of this.operations.values()) {
      if (isSettledStatus(operation.status)) continue
      operation.status = 'unknown'
      operation.error = 'DSH worker 在这一步结束前退出，结果未知；按 operationId 对账，不要重放'
      operation.settledAt ??= Date.now()
      this.append(operation)
      recovered += 1
    }
    return recovered
  }

  /**
   * Read the durable state for an operation identity.
   * @param operationId - operation identity.
   * @returns the stored operation, if any.
   */
  get(operationId: string): QqLifeOperation | undefined {
    return this.operations.get(operationId)
  }

  /**
   * List recorded operations.
   * @returns every stored operation, oldest first.
   */
  all(): QqLifeOperation[] {
    return [...this.operations.values()].sort((left, right) => left.startedAt - right.startedAt)
  }

  /**
   * Insert or replace one operation and flush it to disk.
   * @param operation - current operation state.
   */
  put(operation: QqLifeOperation): void {
    this.operations.set(operation.operationId, operation)
    this.append(operation)
  }

  private append(operation: QqLifeOperation): void {
    mkdirSync(dirname(this.file), { recursive: true })
    appendFileSync(this.file, `${JSON.stringify({ v: 1, op: operation })}\n`)
    this.lines += 1
    if (this.lines > this.operations.size * 4 + 64) this.compact()
  }

  /** Rewrite the log with one line per operation; a failed rewrite keeps the old file. */
  private compact(): void {
    const body = this.all().map(operation => JSON.stringify({ v: 1, op: operation })).join('\n')
    const staging = `${this.file}.tmp`
    try {
      writeFileSync(staging, body.length === 0 ? '' : `${body}\n`)
      renameSync(staging, this.file)
      this.lines = this.operations.size
    } catch {
      // The append-only file is still complete; compaction retries next write.
    }
  }
}
