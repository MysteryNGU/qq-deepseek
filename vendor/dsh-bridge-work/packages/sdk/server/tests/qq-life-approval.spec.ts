import { Context } from '@deepseek-ai/cordis'
import type { ApprovalRequestEvent, ApprovalOutcome } from '@deepseek-ai/dsh-user-approval/types'
import { describe, expect, it, vi } from 'vitest'
import { HarnessSdkJsonRpcServer } from '../src/server.ts'

describe('QQ life reverse approval routing', () => {
  function fixture(outcome: string) {
    const ctx = new Context()
    const on = vi.spyOn(ctx, 'on')
    const transport = { request: vi.fn().mockResolvedValue({ outcome }), notify: vi.fn() }
    const server = new HarnessSdkJsonRpcServer(ctx, transport)
    const agent = { session: { id: 'owned-session' } }
    Object.assign(server, {
      sessions: new Map([['owned-session', { handle: { agent } }]]),
      qqLifeLog: { all: () => [{ operationId: 'op1', sessionId: 'owned-session', tool: 'shell', arguments: { command: 'echo 1' } }] },
      qqLifeInflight: new Map([['op1', Promise.resolve()]]),
    })
    const listener = on.mock.calls.find(call => call[0] === 'approval/request')?.[1] as
      (request: ApprovalRequestEvent, next: () => Promise<ApprovalOutcome>) => Promise<ApprovalOutcome>
    return { agent, listener, transport }
  }

  it('forwards only the owned pending operation and accepts only allowed-once', async () => {
    const { agent, listener, transport } = fixture('allowed-once')
    const request = { agent, toolName: 'shell', callId: 'op1' } as unknown as ApprovalRequestEvent
    expect(await listener(request, () => Promise.resolve('unavailable'))).toBe('allowed-once')
    expect(transport.request).toHaveBeenCalledWith('qq_life/approval', expect.objectContaining({ operationId: 'op1', executionScope: 'owned-session' }))
  })

  it('delegates a foreign agent without exposing its request', async () => {
    const { listener, transport } = fixture('allowed-once')
    const next = vi.fn().mockResolvedValue('unavailable')
    expect(await listener({ agent: { session: { id: 'foreign' } }, toolName: 'shell' } as unknown as ApprovalRequestEvent, next)).toBe('unavailable')
    expect(next).toHaveBeenCalledOnce()
    expect(transport.request).not.toHaveBeenCalled()
  })

  it('does not interpret unknown approval outcomes as permission', async () => {
    const { agent, listener } = fixture('yes')
    expect(await listener({ agent, toolName: 'shell' } as unknown as ApprovalRequestEvent, () => Promise.resolve('unavailable'))).toBe('rejected')
  })
})
